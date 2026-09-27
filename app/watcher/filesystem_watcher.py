"""The folder watcher.

``watchdog`` gives us low-level filesystem events; everything else is
deliberately ours:

* events are **coalesced** and fed into a stability tracker, so a 6 GB copy
  produces exactly one job after it settles, not hundreds;
* the loop is a plain, restartable, time-driven sweep (a watchdog event only
  *wakes* the sweep early), which makes it identical on Windows, Linux and
  macOS and immune to dropped/incoherent inotify events;
* every candidate passes through :func:`pair_directory`, so pairing rules live
  in one place;
* discovery survives restarts because completed work is recorded in SQLite.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.config import AppConfig, ShowConfig
from ..security.redaction import redact_text
from .pairing import EpisodePair, pair_directory
from .stability import StabilityTracker

log = logging.getLogger(__name__)

__all__ = ["FilesystemWatcher", "WatcherEvent", "WatcherStatus", "ShowWatch"]


@dataclass
class WatcherEvent:
    """What the watcher noticed, for the dashboard activity feed."""

    at: float
    show_id: str
    kind: str          # "pair_ready" | "waiting_metadata" | "unstable" | "error" | "scan"
    message: str
    path: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "at": self.at,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.at)),
            "show_id": self.show_id,
            "kind": self.kind,
            "message": redact_text(self.message, max_len=500),
            "path": self.path,
            "detail": self.detail,
        }


@dataclass
class WatcherStatus:
    running: bool = False
    watching: int = 0
    pending_files: int = 0
    last_scan: float = 0.0
    last_event: float = 0.0
    scans: int = 0
    errors: int = 0
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "watching": self.watching,
            "pending_files": self.pending_files,
            "last_scan": time.strftime("%H:%M:%S", time.localtime(self.last_scan)) if self.last_scan else "-",
            "last_scan_ts": self.last_scan,
            "last_event_ts": self.last_event,
            "scans": self.scans,
            "errors": self.errors,
            "details": self.details,
        }


@dataclass
class ShowWatch:
    show: ShowConfig
    incoming: Path
    tracker: StabilityTracker
    #: Files already handed to the consumer (so we never hand over twice).
    emitted: set[tuple[str, int, int]] = field(default_factory=set)
    last_error: str = ""


class FilesystemWatcher:
    """Watches every configured show's ``incoming`` folder.

    ``on_pair`` is called once per ready media+metadata pair.  The watcher
    itself never hashes, validates, or uploads - keeping it minimal is what
    makes it reliable.
    """

    def __init__(
        self,
        config: AppConfig,
        on_pair: Callable[[EpisodePair], None],
        on_event: Callable[[WatcherEvent], None] | None = None,
    ):
        self.config = config
        self.on_pair = on_pair
        self.on_event = on_event
        self.status = WatcherStatus()
        self._watches: dict[str, ShowWatch] = {}
        self._observer = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._recent_events: list[WatcherEvent] = []

    # -- lifecycle ---------------------------------------------------------
    def _build_watches(self) -> dict[str, ShowWatch]:
        watches: dict[str, ShowWatch] = {}
        cfg = self.config.watcher
        for show in self.config.enabled_shows():
            incoming = self.config.show_incoming(show)
            try:
                incoming.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.error("Cannot create incoming folder %s: %s", incoming, redact_text(str(exc)))
                continue
            watches[show.resolved_id()] = ShowWatch(
                show=show,
                incoming=incoming,
                tracker=StabilityTracker(
                    stable_seconds=cfg.stable_seconds,
                    stable_checks=cfg.stable_checks,
                    poll_interval=cfg.poll_interval_seconds,
                    timeout=cfg.stability_timeout_seconds,
                ),
            )
        return watches

    def register_shows(self) -> int:
        """Build the per-show watch list without starting the background loop.

        :meth:`scan_once` documents that it can be driven directly (the API's
        "scan now" button and the test-suite both do), so the folders it sweeps
        have to exist before :meth:`start` is ever called.  Calling this more
        than once is harmless.
        """
        with self._lock:
            if not self._watches:
                self._watches = self._build_watches()
            self.status.watching = len(self._watches)
        return len(self._watches)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._watches = self._build_watches()
            self._stop.clear()
            self._start_observer()
            self._thread = threading.Thread(
                target=self._run, name="folder-watcher", daemon=True
            )
            self._thread.start()
            self.status.running = True
            self.status.watching = len(self._watches)
        self.emit("scan", "", f"Watching {len(self._watches)} incoming folder(s).")

    def _start_observer(self) -> None:
        """Use watchdog purely as a wake-up hint; the loop is sweep-driven."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:  # pragma: no cover - watchdog is a hard dependency
            log.warning("watchdog is not installed; falling back to polling only.")
            return

        watch_ref = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:  # noqa: D401
                if getattr(event, "is_directory", False):
                    return
                watch_ref._wake.set()

        try:
            observer = Observer()
            handler = _Handler()
            for watch in self._watches.values():
                observer.schedule(handler, str(watch.incoming), recursive=False)
            observer.start()
            self._observer = observer
        except Exception as exc:  # noqa: BLE001
            log.warning("Filesystem notifications unavailable (%s); using polling.",
                        redact_text(str(exc)))
            self._observer = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=timeout)
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass
            self._observer = None
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self.status.running = False

    def wake(self) -> None:
        """Ask the loop to scan immediately (used by the API and by tests)."""
        self._wake.set()

    def scan_once(self) -> int:
        """Run a single full sweep.  Returns the number of pairs emitted."""
        emitted = 0
        with self._lock:
            watches = list(self._watches.values())
        for watch in watches:
            try:
                emitted += self._scan_show(watch)
            except Exception as exc:  # noqa: BLE001
                watch.last_error = redact_text(str(exc))
                self.status.errors += 1
                log.exception("Watcher error in %s", watch.incoming)
                self.emit("error", watch.show.resolved_id(),
                          f"Watcher error in {watch.incoming}: {watch.last_error}")
        self.status.last_scan = time.time()
        self.status.scans += 1
        self.status.pending_files = sum(
            len(w.tracker.tracked()) for w in watches
        )
        self.status.details = [
            {
                "show_id": w.show.resolved_id(),
                "show": w.show.name,
                "incoming": str(w.incoming),
                "pending": len(w.tracker.tracked()),
                "exists": w.incoming.is_dir(),
                "last_error": w.last_error,
            }
            for w in watches
        ]
        return emitted

    # -- per-show logic ----------------------------------------------------
    def _scan_show(self, watch: ShowWatch) -> int:
        show = watch.show
        show_id = show.resolved_id()
        policy = self.config.media
        pairs, unpaired, problems = pair_directory(
            watch.incoming,
            policy=policy,
            ignore_suffixes=self.config.watcher.ignore_suffixes,
            allow_media_only=policy.allow_media_only,
            show_id=show_id,
        )
        for problem in problems:
            watch.last_error = problem
            self.emit("error", show_id, problem)

        emitted = 0
        now = time.time()

        for pair in pairs:
            fingerprint = (str(pair.media_path), pair.media.size, pair.media.mtime_ns)
            if fingerprint in watch.emitted:
                continue
            media_state = watch.tracker.poll(pair.media_path)
            if not watch.tracker.is_stable(media_state):
                reason = watch.tracker.reason_not_stable(media_state)
                self.emit("unstable", show_id,
                          f"Waiting for {pair.media_path.name}: {reason}.",
                          str(pair.media_path), state=media_state.as_dict())
                continue
            if pair.metadata_path is not None:
                meta_state = watch.tracker.poll(pair.metadata_path)
                if not watch.tracker.is_stable(meta_state):
                    reason = watch.tracker.reason_not_stable(meta_state)
                    self.emit("unstable", show_id,
                              f"Waiting for {pair.metadata_path.name}: {reason}.",
                              str(pair.metadata_path), state=meta_state.as_dict())
                    continue
            watch.emitted.add(fingerprint)
            self.status.last_event = now
            self.emit(
                "pair_ready", show_id,
                f"Found {pair.media_path.name}"
                + (f" + {pair.metadata_path.name}" if pair.metadata_path else " (no metadata file)"),
                str(pair.media_path), pair=pair.as_dict(),
            )
            try:
                self.on_pair(pair)
                emitted += 1
            except Exception as exc:  # noqa: BLE001
                watch.last_error = redact_text(str(exc))
                self.status.errors += 1
                log.exception("on_pair handler failed for %s", pair.media_path)
                self.emit("error", show_id,
                          f"Could not queue {pair.media_path.name}: {watch.last_error}",
                          str(pair.media_path))

        # Media without a .txt yet: keep watching, tell the user what is needed.
        for candidate in unpaired:
            watch.tracker.poll(candidate.path)
            existing = watch.emitted
            key = (str(candidate.path), candidate.size, candidate.mtime_ns)
            if key in existing:
                continue
            self.emit(
                "waiting_metadata", show_id,
                f"{candidate.path.name} has no matching .txt file. Add "
                f"{candidate.path.stem}.txt next to it with TITLE= and DESCRIPTION=.",
                str(candidate.path),
            )
        return emitted

    # -- housekeeping ------------------------------------------------------
    def _prune(self) -> None:
        """Drop stability state for files that no longer exist."""
        with self._lock:
            for watch in self._watches.values():
                for state in watch.tracker.tracked():
                    if not state.path.exists():
                        watch.tracker.forget(state.path)
                # Bound the emitted set so a long-running install cannot grow
                # without limit; entries are only useful while the file exists.
                if len(watch.emitted) > 5000:
                    watch.emitted = set(list(watch.emitted)[-2000:])

    def _run(self) -> None:
        log.debug("Watcher loop started for %d show(s)", len(self._watches))
        first = True
        while not self._stop.is_set():
            try:
                if first and self.config.watcher.rescan_on_start:
                    self.emit("scan", "", "Startup scan of all incoming folders.")
                self.scan_once()
                first = False
            except Exception:  # noqa: BLE001 - the loop must never die
                self.status.errors += 1
                log.exception("Unexpected watcher loop error")
            self._prune()
            timeout = max(0.2, self.config.watcher.poll_interval_seconds)
            self._wake.wait(timeout)
            self._wake.clear()

    def emit(self, kind: str, show_id: str, message: str, path: str = "", **detail) -> None:
        event = WatcherEvent(
            at=time.time(), show_id=show_id, kind=kind,
            message=message, path=path, detail=detail,
        )
        self._recent_events.append(event)
        del self._recent_events[:-500]
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001  # pragma: no cover
                log.debug("Watcher event handler failed", exc_info=True)

    def recent_events(self, limit: int = 100) -> list[dict]:
        return [e.as_dict() for e in self._recent_events[-limit:]][::-1]
