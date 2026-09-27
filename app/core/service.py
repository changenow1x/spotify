"""The application container.

``AutomationService`` owns every long-lived object and is the single place the
CLI, the HTTP API and the tests all go through, so behaviour cannot drift
between the dashboard and the command line.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Optional

from ..archive.manager import ArchiveManager
from ..publishers.base import PublisherAdapter
from ..publishers.registry import get_registry, known_adapters
from ..publishers.spotify.publisher import SpotifyPublisher
from ..security.paths import sanitize_stem
from ..watcher.filesystem_watcher import FilesystemWatcher, WatcherEvent
from ..watcher.pairing import EpisodePair
from .config import AppConfig
from .database import Database, JobRow
from .hashing import content_hash, hash_path
from .logging_setup import log_activity
from .queue import JobWorker, WorkerPool
from .state_machine import JobState

#: States a job may be manually re-queued from.  Anything else -- notably
#: PUBLISHED / SCHEDULED / ARCHIVED -- is finished, and re-queueing it would
#: discard the confirmed publication evidence and risk a second upload.
RETRYABLE_STATES: frozenset[JobState] = frozenset({
    JobState.FAILED, JobState.RETRY_WAIT, JobState.NEEDS_USER_ACTION,
    JobState.NEEDS_RECONCILIATION, JobState.CANCELLED,
})

log = logging.getLogger(__name__)

__all__ = ["AutomationService", "build_service", "register_builtin_adapters"]


def register_builtin_adapters(config: AppConfig, database: Database) -> None:
    registry = get_registry()
    registry.register(
        "spotify",
        lambda: SpotifyPublisher(config, database=database),
    )


class AutomationService:
    def __init__(
        self,
        config: AppConfig,
        *,
        adapter: PublisherAdapter | None = None,
        ensure_dirs: bool = True,
    ):
        self.config = config
        if ensure_dirs:
            config.ensure_directories()
        self.db = Database(config.database_path())
        self.archive = ArchiveManager(config)
        if adapter is not None:
            from ..publishers.registry import get_registry as _gr
            _gr().register("spotify", lambda: adapter)
        register_builtin_adapters(config, self.db)
        self.worker_pool = WorkerPool(config, self.db, size=config.max_concurrent_jobs, adapter=adapter)
        self.watcher = FilesystemWatcher(config, on_pair=self.on_pair_discovered,
                                         on_event=self.on_watcher_event)
        self.started_at = 0.0
        self._events: list[dict] = []
        self._event_lock = threading.Lock()
        self._progress: dict[str, dict] = {}
        self._stopping = False
        self._auth_thread: Optional[threading.Thread] = None
        self._login_state: dict = {"state": "idle", "message": "", "result": None}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self.started_at:
            return
        self.started_at = time.time()
        recovered = self.db.recover_interrupted_jobs()
        released = self.db.reset_stale_claims()
        if recovered or released:
            log_activity(
                f"Recovered {len(recovered)} job(s) left behind by an unclean shutdown; "
                f"they will be reconciled with Spotify before anything is uploaded."
            )
        self.watcher.start()
        self.worker_pool.start()
        self.db.audit("service.start", detail=f"root={self.config.root_path()}")
        log_activity("PodcastAutomation started.")
        log.info("Service started. Root=%s Dashboard=http://%s:%s",
                 self.config.root_path(), self.config.api.host, self.config.api.port)

    def stop(self, timeout: float = 20.0) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.watcher.stop()
        self.worker_pool.stop(timeout=timeout)
        try:
            get_registry().close_all()
        except Exception:  # noqa: BLE001  # pragma: no cover
            log.debug("Error closing adapters", exc_info=True)
        self.db.audit("service.stop")
        log_activity("PodcastAutomation stopped.")
        self._stopping = False
        self.started_at = 0.0

    # -- discovery --------------------------------------------------------
    def on_watcher_event(self, event: WatcherEvent) -> None:
        payload = event.as_dict()
        with self._event_lock:
            self._events.append(payload)
            del self._events[:-400]
        if event.kind in {"pair_ready", "error"}:
            self.db.log_event(None, None, "warning" if event.kind == "error" else "info",
                              event.message, {"show_id": event.show_id})

    def on_pair_discovered(self, pair: EpisodePair) -> JobRow | None:
        """Turn a detected pair into a persisted job (idempotently)."""
        show_id = pair.show_id
        media_path = pair.media_path
        if not media_path.is_file():
            log.warning("Discovered %s but it vanished before it could be queued.", media_path)
            return None
        try:
            media_hash = hash_path(media_path, mode=self.config.hash_mode).digest
        except OSError as exc:
            log_activity(f"Could not read {media_path.name}: {exc}")
            self.db.audit("discover.failed", str(media_path), str(exc))
            return None

        metadata_hash = ""
        metadata_path = pair.metadata_path
        if metadata_path and metadata_path.is_file():
            try:
                from .hashing import hash_text
                metadata_hash = hash_text(metadata_path.read_bytes().decode("utf-8", errors="replace"))
            except OSError:  # pragma: no cover
                metadata_hash = ""

        self.db.note_seen_file(show_id, str(media_path), "media",
                               media_path.stat().st_size, media_path.stat().st_mtime_ns,
                               media_hash)
        if metadata_path:
            self.db.note_seen_file(show_id, str(metadata_path), "metadata",
                                   metadata_path.stat().st_size,
                                   metadata_path.stat().st_mtime_ns)

        job_id = str(uuid.uuid4())
        job = self.db.create_job(
            id=job_id,
            show_id=show_id,
            job_type=pair.job_type,
            base_name=sanitize_stem(pair.base_name),
            original_filename=media_path.name,
            media_path=str(media_path),
            metadata_path=str(metadata_path) if metadata_path else None,
            media_size=media_path.stat().st_size,
            media_mtime_ns=media_path.stat().st_mtime_ns,
            media_hash=media_hash,
            metadata_hash=metadata_hash or None,
            content_hash=content_hash(media_hash, metadata_hash or None),
            spotify_show_id=(self.config.get_show(show_id).spotify_show_id
                             if self.config.get_show(show_id) else "") or None,
        )
        if job is None:
            return None
        if job["id"] != job_id:
            # create_job returned the pre-existing duplicate job.
            self.db.log_event(job["id"], JobState(job["state"]), "info",
                              f"Attached the re-dropped file to this existing job "
                              f"(original: {job['original_filename']}).")
            return job
        self.db.remove_pending_media(show_id, str(media_path))
        self.db.audit("job.created", job["id"], f"{media_path.name} for show {show_id}")
        log_activity(
            f"Detected episode {pair.media.path.name} for "
            f"{self._show_name(show_id)}."
        )
        return job

    def _show_name(self, show_id: str) -> str:
        show = self.config.get_show(show_id)
        return show.name if show else show_id

    # -- manual actions (dashboard / CLI) -----------------------------------
    def retry_job(self, job_id: str, *, now: bool = True) -> Optional[JobRow]:
        job = self.db.get_job(job_id)
        if job is None:
            return None
        state = job.state_enum()
        if state not in RETRYABLE_STATES:
            # A published/archived job is finished. Re-queueing it would clear
            # the upload evidence and hand a live episode back to the pipeline,
            # so the retry is refused rather than silently degrading the row.
            self.db.audit("job.retry_refused", job_id, f"from {state.value}")
            log_activity(
                f"Retry refused: {job['original_filename']} is {state.value} "
                "and cannot be re-queued."
            )
            return None
        self.db.audit("job.retry", job_id, f"from {state.value}")
        fields: dict[str, Any] = {
            "next_attempt_at": None,
            "last_error": None,
            "cancel_requested": 0,
            "claimed_by": None,
        }
        # A validation failure needs re-validation; anything else resumes the
        # pipeline, but reconciliation always runs first for remote states.
        if state in {JobState.FAILED, JobState.NEEDS_USER_ACTION}:
            if "Validation failed" in (job.get("last_error") or "") or not job.get("media_hash"):
                fields.update({"media_hash": None, "title": ""})
        self.db.update_job(job_id, **fields)
        return self.db.transition(
            job_id, JobState.RETRY_WAIT, force=True,
            message="Retrying on request." if now else "Queued for retry.",
            retry_count=int(job.get("retry_count") or 0),
        )

    def cancel_job(self, job_id: str) -> bool:
        job = self.db.get_job(job_id)
        if job is None:
            return False
        self.db.request_cancel(job_id)
        self.db.audit("job.cancel", job_id)
        log_activity(f"Cancellation requested for {job['original_filename']}.")
        return True

    def force_publish(self, job_id: str) -> Optional[JobRow]:
        """User override: publish even though validation produced warnings."""
        job = self.db.get_job(job_id)
        if job is None:
            return None
        self.db.audit("job.force", job_id)
        self.db.update_job(job_id, last_error=None, next_attempt_at=None)
        return self.db.transition(job_id, JobState.RETRY_WAIT, force=True,
                                  message="User forced publication despite the warnings.")

    def archive_now(self, job_id: str) -> bool:
        job = self.db.get_job(job_id)
        if job is None or job.state_enum() not in {JobState.PUBLISHED, JobState.SCHEDULED}:
            return False
        worker = self.worker_pool._workers[0] if self.worker_pool._workers else \
            JobWorker(self.config, self.db, adapter=self._adapter_or_none())
        worker._archive(job_id)
        return self.db.get_job(job_id).state_enum() is JobState.ARCHIVED

    def _adapter_or_none(self):
        try:
            return get_registry().get("spotify")
        except KeyError:  # pragma: no cover
            return None

    def set_paused(self, paused: bool) -> bool:
        self.config.pause_queue = bool(paused)
        self.db.set_setting("queue.paused", bool(paused))
        self.db.audit("queue.pause" if paused else "queue.resume")
        log_activity(
            "The queue is paused. New files are still detected and will wait."
            if paused else "The queue is running again."
        )
        return self.config.pause_queue

    def queue_paused(self) -> bool:
        stored = self.db.get_setting("queue.paused")
        if stored is not None:
            self.config.pause_queue = bool(stored)
        return self.config.pause_queue

    # -- status -------------------------------------------------------------
    def status(self) -> dict:
        self.queue_paused()
        counts = self.db.count_jobs()
        try:
            adapter = get_registry().get("spotify")
            spotify_status = adapter.get_status().as_dict()
            capabilities = adapter.capabilities().as_dict()
        except Exception as exc:  # noqa: BLE001
            spotify_status = {"ok": False, "summary": f"Adapter unavailable: {exc}",
                              "details": [], "problems": []}
            capabilities = {}
        return {
            "app": {
                "running": bool(self.started_at),
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
                "version": __import__("app").__version__,
                "root": str(self.config.root_path()),
                "queue_paused": self.config.pause_queue,
                "jobs_processed": self.worker_pool.processed,
            },
            "watcher": self.watcher.status.as_dict(),
            "workers": self.worker_pool.snapshot(),
            "spotify": spotify_status,
            "capabilities": capabilities,
            "adapters": known_adapters(),
            "shows": self.shows_status(),
            "counts": counts,
            "database": self.db.stats(),
            "platform": {"system": _platform_name(), "python": _python_version()},
        }

    def shows_status(self) -> list[dict]:
        out = []
        for show in self.config.shows:
            incoming = self.config.show_incoming(show)
            show_id = show.resolved_id()
            try:
                file_count = len([p for p in incoming.iterdir() if p.is_file()]) if incoming.is_dir() else 0
            except OSError:  # pragma: no cover
                file_count = -1
            counts = self.db.count_jobs(show_id=show_id)
            out.append({
                "id": show_id,
                "name": show.name,
                "enabled": show.enabled,
                "incoming": str(incoming),
                "incoming_exists": incoming.is_dir(),
                "incoming_files": file_count,
                "archive": str(self.config.show_path(show, "archive")),
                "failed": str(self.config.show_path(show, "failed")),
                "spotify_show_id": show.spotify_show_id,
                "spotify_show_name": show.spotify_show_name,
                "timezone": show.timezone,
                "defaults": show.defaults.__dict__,
                "counts": counts,
                "archived_bytes": self.archive.archive_size(show),
            })
        return out

    def jobs(self, *, show_id: str = "", state: str = "", limit: int = 100) -> list[dict]:
        pass

        states = None
        if state:
            states = [s.strip() for s in state.split(",") if s.strip()]
        rows = self.db.list_jobs(show_id=show_id or None, states=states, limit=limit)
        return [self.job_view(row) for row in rows]

    def job_view(self, job: JobRow) -> dict:
        from .state_machine import STATE_HINTS, STATE_LABELS

        state = job.state_enum()
        media_info = job.media_info()
        metadata = job.metadata()
        diagnostics = job.diagnostics()
        return {
            "id": job["id"],
            "show_id": job["show_id"],
            "show_name": self._show_name(job["show_id"]),
            "state": state.value,
            "state_label": STATE_LABELS.get(state, state.value),
            "state_hint": STATE_HINTS.get(state, ""),
            "job_type": job.get("job_type"),
            "filename": job["original_filename"],
            "base_name": job["base_name"],
            "title": job.get("title") or "(not validated yet)",
            "media_path": job["media_path"],
            "metadata_path": job.get("metadata_path"),
            "media_kind": job.get("media_kind"),
            "size": job.get("media_size"),
            "duration": media_info.get("duration_hms"),
            "resolution": (f"{media_info.get('width')}x{media_info.get('height')}"
                           if media_info.get("width") else None),
            "season": job.get("season"),
            "episode_number": job.get("episode_number"),
            "explicit": bool(job.get("is_explicit")),
            "publish_mode": job.get("publish_mode"),
            "publish_at": job.get("publish_at_utc"),
            "timezone": job.get("timezone"),
            "publish_local": metadata.get("publish_at"),
            "chapters": len(metadata.get("chapters", []) or []),
            "spotify_episode_id": job.get("spotify_episode_id"),
            "spotify_url": job.get("spotify_url"),
            "spotify_state": job.get("spotify_state"),
            "progress": job.get("progress_percent", 0),
            "status_message": job.get("status_message"),
            "last_error": job.get("last_error"),
            "retry_count": job.get("retry_count"),
            "next_attempt_at": job.get("next_attempt_at"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "published_at": job.get("publish_confirmed_at"),
            "archived_at": job.get("archived_at"),
            "archived_path": job.get("archived_media_path"),
            "media_hash": (job.get("media_hash") or "")[:12],
            "content_hash": (job.get("content_hash") or "")[:12],
            "cancel_requested": bool(job.get("cancel_requested")),
            "diagnostics": diagnostics if diagnostics else None,
            "can_retry": state in RETRYABLE_STATES,
            "can_cancel": state.value not in {"ARCHIVED", "PUBLISHED", "SCHEDULED",
                                              "CANCELLED", "FAILED"},
            "can_archive": state.value in {"PUBLISHED", "SCHEDULED"} and not job.get("archived_media_path"),
            "progress_live": self._progress.get(job["id"]),
        }

    def events(self, limit: int = 80) -> list[dict]:
        with self._event_lock:
            items = list(self._events)[-limit:]
        return items[::-1]

    def log_tail(self, lines: int = 200) -> list[str]:
        from .logging_setup import activity_tail
        return activity_tail(lines)

    def register_progress(self, hook) -> None:
        for worker in self.worker_pool._workers:
            worker.on_progress(hook)

    # -- interactive Spotify sign-in ---------------------------------------
    def start_spotify_login(self, *, timeout_seconds: int = 900) -> dict:
        """Open a real browser window and let the user sign in, once.

        Runs on a background thread so the dashboard stays responsive.  The
        user types their password into Spotify's own form; this process never
        receives, stores or logs it.
        """
        from ..publishers.spotify.publisher import SpotifyPublisher

        if self.spotify_login_running():
            return {"started": False, "already_running": True,
                    "message": "A sign-in window is already open."}
        try:
            publisher = get_registry().get("spotify")
        except KeyError as exc:  # pragma: no cover
            return {"started": False, "error": str(exc)}
        if not isinstance(publisher, SpotifyPublisher):
            return {"started": False, "error": "The active adapter is not the Spotify adapter."}

        with self._event_lock:
            self._login_state = {
                "state": "running",
                "message": ("A Spotify window has opened. Sign in there - this app never "
                            "sees your password. If Spotify asks for a verification code, "
                            "enter it in that window too."),
                "started_at": time.time(),
                "result": None,
            }

        def _run() -> None:
            def on_wait(message: str) -> None:
                with self._event_lock:
                    if self._login_state.get("state") == "running":
                        self._login_state["message"] = message
                log_activity(message)

            try:
                session = publisher.session
                result = publisher.auth.interactive_login(
                    session, timeout_seconds=timeout_seconds, on_wait=on_wait
                )
                with self._event_lock:
                    self._login_state.update({
                        "state": "done",
                        "result": result.as_dict(),
                        "message": result.message,
                        "finished_at": time.time(),
                    })
                log_activity(f"Spotify sign-in finished: {result.message}")
                self.db.audit("spotify.login", detail=result.status.value)
            except Exception as exc:  # noqa: BLE001
                from ..security.redaction import scrub_exception
                with self._event_lock:
                    self._login_state.update({
                        "state": "failed",
                        "result": {"status": "FAILED", "message": scrub_exception(exc)},
                        "message": f"Sign-in could not be started: {scrub_exception(exc)}",
                        "finished_at": time.time(),
                    })

        thread = threading.Thread(target=_run, name="spotify-login", daemon=True)
        self._auth_thread = thread
        thread.start()
        return {"started": True, "message": "A Spotify sign-in window is opening.",
                "login_url": self.config.spotify.login_url}

    def spotify_login_running(self) -> bool:
        thread = self._auth_thread
        return bool(thread and thread.is_alive())

    def spotify_login_state(self) -> dict:
        with self._event_lock:
            return dict(self._login_state)


def build_service(config: AppConfig, *, adapter: PublisherAdapter | None = None,
                  ensure_dirs: bool = True) -> AutomationService:
    return AutomationService(config, adapter=adapter, ensure_dirs=ensure_dirs)


def _platform_name() -> str:
    import platform
    return f"{platform.system()} {platform.release()}"


def _python_version() -> str:
    import platform
    return platform.python_version()
