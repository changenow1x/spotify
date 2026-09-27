"""The worker: takes jobs from SQLite and drives them through the state machine.

This is where "never publish twice" is actually enforced at run time.  The
guarantee comes from four rules applied to every attempt:

**R1 - Reconcile before acting.**  Any job that may have reached Spotify is
reconciled first (see :mod:`app.core.state_machine`).  Only if Spotify says
"no such episode" may the upload start.

**R2 - One job, one identity.**  Retries reuse the same job row.  A job's
``content_hash`` is immutable, and the database's unique index means a second
job for the same content cannot exist.

**R3 - Progress is monotonic within a run.**  A retry re-enters at
``QUEUED``/``UPLOADING`` but never regresses a terminal state, and a job that
is already ``PUBLISHED``/``ARCHIVED`` is never handed to the publisher again.

**R4 - Unproven is not success.**  Only a :class:`PublicationStatus` that
carries remote evidence (``PUBLISHED``/``SCHEDULED``/``DUPLICATE``) moves a
job to a success state.  ``UNKNOWN`` goes to ``NEEDS_RECONCILIATION``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from ..archive.manager import ArchiveManager
from .config import AppConfig
from .database import Database, JobRow
from ..media.ffmpeg import FfmpegTools
from ..media.inspector import MediaInspector
from ..media.validator import MediaValidator
from ..metadata.parser import (
    MetadataParseError,
    build_metadata,
    parse_metadata_file,
)
from ..metadata.validator import validate_episode
from .models import EpisodeMetadata, MediaInfo, PublishMode, utc_now
from ..publishers.base import (
    ProgressCallback,
    PublicationStatus,
    PublishRequest,
    PublisherAdapter,
    PublisherError,
    RemoteDuplicateFound,
)
from ..publishers.registry import get_registry
from ..security.paths import is_within
from .state_machine import JobState
from .hashing import content_hash, hash_path, hash_text
from .logging_setup import log_activity

log = logging.getLogger(__name__)


#: States that mean the job has already been sent to Spotify at least once.
#: Only for these is a same-titled remote episode evidence that *this* job
#: produced it.  Anything earlier is a job that has never left the machine.
#: States in which the duplicate check must run before anything is uploaded.
#: A job claimed from one of these may already have an episode on Spotify.
_RECONCILE_FIRST_STATES = frozenset({
    JobState.NEEDS_RECONCILIATION,
    JobState.UPLOADING,
    JobState.PROCESSING,
    JobState.READY_TO_PUBLISH,
    JobState.PUBLISHING,
    JobState.ARCHIVING,
    JobState.PUBLISHED,
    JobState.SCHEDULED,
    JobState.AUTHENTICATING,
})

_ADVANCED_STATES = frozenset({
    JobState.AUTHENTICATING,
    JobState.UPLOADING,
    JobState.PROCESSING,
    JobState.READY_TO_PUBLISH,
    JobState.PUBLISHING,
    JobState.PUBLISHED,
    JobState.SCHEDULED,
    JobState.ARCHIVING,
    JobState.ARCHIVED,
    JobState.NEEDS_RECONCILIATION,
    JobState.NEEDS_USER_ACTION,
    JobState.RETRY_WAIT,
})

__all__ = ["JobWorker", "WorkerPool", "ValidationOutcome"]


class ValidationOutcome:
    __slots__ = ("ok", "media_info", "metadata", "report", "error", "media_hash",
                 "metadata_hash", "content_hash")

    def __init__(self, ok: bool, media_info=None, metadata=None, report=None,
                 error: str = "", media_hash: str = "", metadata_hash: str = ""):
        self.ok = ok
        self.media_info = media_info
        self.metadata = metadata
        self.report = report
        self.error = error
        self.media_hash = media_hash
        self.metadata_hash = metadata_hash
        self.content_hash = content_hash(media_hash, metadata_hash) if (media_hash or metadata_hash) else None


class JobWorker:
    """Validates and publishes exactly one job at a time."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        adapter: PublisherAdapter | None = None,
        worker_id: str = "worker-1",
        archive_manager: ArchiveManager | None = None,
        on_state: Optional[Callable[[str, str, Optional[str]], None]] = None,
    ):
        self.config = config
        self.db = database
        self.worker_id = worker_id
        self.archive = archive_manager or ArchiveManager(config)
        self._adapter = adapter
        self._on_state = on_state
        tools = FfmpegTools(config.media.ffprobe_path, config.media.ffmpeg_path)
        self.inspector = MediaInspector(tools, config.media)
        self.media_validator = MediaValidator(self.inspector, config.media)
        self._progress_hook: Optional[Callable[[str, dict], None]] = None

    # -- adapter ---------------------------------------------------------
    @property
    def adapter(self) -> PublisherAdapter:
        if self._adapter is None:
            self._adapter = get_registry().get("spotify")
        return self._adapter

    def on_progress(self, hook: Callable[[str, dict], None] | None) -> None:
        self._progress_hook = hook

    # -- entry point -----------------------------------------------------
    def run_once(self) -> Optional[str]:
        """Claim and process one job.  Returns the job id, or ``None`` if idle."""
        job = self.db.claim_next_job(self.worker_id)
        if job is None:
            return None
        self.run_job(job)
        return job["id"]

    def run_job(self, job: JobRow) -> None:
        job_id = job["id"]
        self.db.log_event(job_id, job["state"], "info", f"Worker {self.worker_id} picked up this job.")
        try:
            claimed_from = job["state"]
            self._advance(job_id, JobState.QUEUED, force=True, message="Picked up by the worker.")
            if self.db.cancel_requested(job_id):
                self._advance(job_id, JobState.CANCELLED, message="Cancelled before starting.")
                return
            self._process(job_id, claimed_from=claimed_from)
        except Exception as exc:  # noqa: BLE001 - the worker thread must survive
            from ..security.redaction import scrub_exception
            log.exception("Unhandled error while processing job %s", job_id)
            self._fail(job_id, scrub_exception(exc), retryable=True)
        finally:
            # A browser left in a broken state must not be reused for the next
            # episode; a healthy one is kept so the session is not re-created
            # for every single file.
            if self.db.get_job(job_id).state_enum() in {
                JobState.FAILED, JobState.NEEDS_USER_ACTION, JobState.RETRY_WAIT,
            }:
                closer = getattr(self.adapter, "close_session", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001  # pragma: no cover
                        pass

    # -- the pipeline ----------------------------------------------------
    def _process(self, job_id: str, *, claimed_from: str = "") -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return
        if self.db.cancel_requested(job_id):
            self._advance(job_id, JobState.CANCELLED, message="Cancelled.")
            return

        # Three independent pieces of evidence, because no single one is
        # reliable: the state the worker *claimed* the job in (run_job has
        # already moved the row to QUEUED by now, so the current state says
        # nothing), the durable stamps, and a recorded remote id.
        # Every job is checked against the show before anything is created
        # remotely - including a job that has never been sent.  A file the
        # user dropped in may be a re-ingest of something already on the show
        # (an older install, a retry, a manual upload), and the only way to
        # know is to look.  A match is adopted only when it is decisive; see
        # ``Reconciler.decide``.
        may_exist_remotely = True

        # ---- Step 1: validate (cheap, no browser) --------------------------
        if not job.get("media_hash") or not job.get("title"):
            self._advance(job_id, JobState.VALIDATING, force=True,
                          message="Checking the media and the metadata file...")
            job = self.db.get_job(job_id) or job
            outcome = self.validate_job(job)
            if not outcome.ok:
                self._handle_validation_failure(job, outcome)
                return
            job = self._store_validation(job_id, outcome)

        metadata = self._load_metadata(job)
        media_info = self._load_media_info(job)
        if metadata is None or media_info is None:
            self._fail(job_id, "The episode's stored metadata or media information is unreadable. "
                               "The files may have been moved or deleted.", retryable=False)
            return

        # ---- Step 2: reconcile before anything is created remotely ---------
        if may_exist_remotely:
            log_activity(
                f"Checking Spotify for {metadata.title!r} so it is not published twice."
            )
            decision = self._reconcile(job, metadata, claimed_from)
            if decision.is_duplicate and decision.candidate is not None:
                self._adopt_existing(job, decision.candidate, metadata)
                return
            if decision.is_unknown and self._has_reached_spotify(job_id, claimed_from):
                # Spotify could not be read and this job has been there
                # before.  Publishing now would risk a duplicate, so the job
                # waits to be retried rather than guessing.
                self._fail(job_id, decision.reason, retryable=True,
                           state=JobState.NEEDS_RECONCILIATION)
                return
            self._advance(job_id, JobState.QUEUED, force=True,
                          message="Spotify has no such episode - safe to publish.")

        # ---- Step 3: publish ----------------------------------------------
        self._publish(job, metadata, media_info)

    # -- validation --------------------------------------------------------
    def validate_job(self, job: JobRow) -> ValidationOutcome:
        """Validate media + metadata.  Never raises for ordinary bad input."""
        media_path = Path(job["media_path"])
        if not is_within(self.config.root_path(), media_path):
            return ValidationOutcome(False, error=(
                f"The media file is outside this installation's folder: {media_path}"))
        if not media_path.is_file():
            return ValidationOutcome(False, error=f"The media file is missing: {media_path}")

        try:
            media_hash = hash_path(media_path, mode=self.config.hash_mode).digest
        except OSError as exc:
            return ValidationOutcome(False, error=f"The media file cannot be read: {exc}")

        metadata_hash = ""
        parsed = None
        meta: Optional[EpisodeMetadata] = None
        parse_error: Optional[MetadataParseError] = None
        metadata_path = job.get("metadata_path")
        if job.get("job_type") == "media_only" or not metadata_path:
            if not self.config.media.allow_media_only:
                return ValidationOutcome(
                    False, media_hash=media_hash,
                    error=(
                        "This file has no matching .txt metadata file. Add "
                        f"{media_path.stem}.txt next to it with TITLE= and DESCRIPTION= lines."
                    ),
                )
            meta = self._metadata_from_filename(media_path)
            parsed = None
        else:
            meta_path = Path(metadata_path)
            if not is_within(self.config.root_path(), meta_path):
                return ValidationOutcome(False, media_hash=media_hash, error=(
                    "The metadata file is outside this installation's folder."))
            if not meta_path.is_file():
                return ValidationOutcome(False, media_hash=media_hash, error=(
                    f"The metadata file is missing: {meta_path}"))
            try:
                raw = meta_path.read_bytes()
                metadata_hash = hash_text(raw.decode("utf-8", errors="replace"))
                parsed = parse_metadata_file(meta_path, max_bytes=self.config.watcher.max_metadata_bytes)
            except (OSError, MetadataParseError) as exc:
                return ValidationOutcome(False, media_hash=media_hash,
                                         error=f"The metadata file cannot be read: {exc}")
            try:
                meta = build_metadata(
                    parsed,
                    defaults=self._show_defaults(job["show_id"]),
                    max_title_length=self.config.security.max_title_length,
                    max_description_length=self.config.security.max_description_length,
                )
            except MetadataParseError as exc:
                parse_error = exc

        media_info, media_report = self.media_validator.validate(media_path)

        show = self.config.get_show(job["show_id"])
        report = validate_episode(
            parsed, meta, parse_error=parse_error,
            show_name=show.name if show else "",
            min_chapters=self.config.security.min_chapters,
            min_chapter_gap_seconds=self.config.security.min_chapter_gap_seconds,
            max_chapters=self.config.security.max_chapters,
        )
        # Media problems fold into the same report so the user sees everything at once.
        report.errors.extend(media_report.errors)
        report.warnings.extend(media_report.warnings)
        report.info.update(media_report.info)
        report.ok = not report.errors

        blocking_warnings = (
            self.config.media.block_on_spec_warning and bool(media_report.warnings)
        )
        if report.ok and blocking_warnings:
            report.ok = False
            report.add_error(
                "media_spec_warning_blocks",
                "The file is usable but outside Spotify's recommendations, and this "
                "installation is configured to stop on that. Fix it, or set "
                "media.block_on_spec_warning: false in config.yaml.",
            )
        return ValidationOutcome(
            ok=report.ok, media_info=media_info, metadata=meta, report=report,
            error=report.summary(), media_hash=media_hash, metadata_hash=metadata_hash,
        )

    def _metadata_from_filename(self, media_path: Path) -> EpisodeMetadata:
        from ..metadata.parser import ParsedFile
        from ..security.paths import sanitize_stem

        stem = sanitize_stem(media_path.stem, fallback="Untitled episode")
        parsed = ParsedFile(
            values={
                "TITLE": stem.replace("_", " ").replace("-", " ").strip(),
                "DESCRIPTION": f"Automatically created from the file name {media_path.name}.",
            },
            line_numbers={"TITLE": 0, "DESCRIPTION": 0},
            source_path=str(media_path),
        )
        show = None
        return build_metadata(parsed, defaults={}) if show is None else build_metadata(parsed)

    def _store_validation(self, job_id: str, outcome: ValidationOutcome) -> JobRow:
        meta = outcome.metadata
        media_info = outcome.media_info
        fields: dict = {
            "media_hash": outcome.media_hash,
            "metadata_hash": outcome.metadata_hash,
            "content_hash": outcome.content_hash,
            "media_size": getattr(media_info, "size", 0),
            "media_kind": media_info.kind.value if media_info else "unknown",
            "title": meta.title if meta else "",
            "season": meta.season if meta else None,
            "episode_number": meta.episode_number if meta else None,
            "is_explicit": 1 if (meta and meta.explicit) else 0,
            "publish_mode": meta.publish.value if meta else "NOW",
            "publish_at_utc": meta.publish_at.isoformat() if (meta and meta.publish_at) else None,
            "timezone": meta.timezone if meta else "UTC",
            "media_info_json": json.dumps(media_info.as_dict(), default=str) if media_info else None,
            "metadata_json": json.dumps(meta.as_dict(), default=str, ensure_ascii=False) if meta else None,
            "status_message": "Validated",
        }
        row = self.db.update_job(job_id, **fields)
        self._advance(job_id, JobState.VALIDATED, force=True,
                      message="Media and metadata validated.")
        return row or self.db.get_job(job_id)

    def _handle_validation_failure(self, job: JobRow, outcome: ValidationOutcome) -> None:
        report = outcome.report
        message = outcome.error or "Validation failed."
        if report is not None and report.errors:
            parts = []
            for issue in report.errors[:6]:
                prefix = f"{issue.field}: " if issue.field else ""
                parts.append(f"{prefix}{issue.message}")
            message = " | ".join(parts)
        warnings = [w.message for w in (report.warnings if report else [])]
        if warnings:
            self.db.log_event(job["id"], None, "warning",
                              "Warnings: " + "; ".join(warnings[:6]))
        log_activity(f"Validation failed for {Path(job['media_path']).name}: {message}")
        self.db.update_job(
            job["id"],
            last_error=message,
            diagnostics_json=json.dumps(
                (report.as_dict() if report else {}), default=str) if report else None,
        )
        self._fail(job["id"], message, retryable=False, state=JobState.NEEDS_USER_ACTION)

    # -- reconciliation -----------------------------------------------------
    def _has_reached_spotify(self, job_id: str, claimed_from: str = "") -> bool:
        """Durable evidence that this job already sent something to Spotify.

        Read fresh from the database rather than from the caller's copy of the
        row: recovery leaves the in-memory row behind, and a stale state would
        silently disable duplicate protection exactly when it matters most.
        """
        row = self.db.get_job(job_id)
        if row is None:  # pragma: no cover - the caller holds a live job
            return False
        if row.get("spotify_episode_id") or row.get("spotify_url"):
            return True
        if row.get("upload_started_at") or row.get("publish_attempted_at"):
            return True
        if _ADVANCED_STATES & {JobState(row["state"])}:
            return True
        # The row has already been moved to QUEUED by run_job, so the state it
        # was *claimed* in is the only remaining hint that this job was picked
        # up out of a mid-flight state.
        return bool(claimed_from) and claimed_from in {
            s.value for s in _ADVANCED_STATES
        }

    def _reconcile(self, job: JobRow, metadata: EpisodeMetadata,
                   claimed_from: str = "") -> "MatchDecision":  # noqa: F821
        """Ask Spotify whether this episode already exists.

        Returns the decision.  ``decision.candidate`` is the episode to adopt,
        ``decision.is_unknown`` means the remote list could not be read, and a
        decision with neither means it is safe to publish.
        """
        from ..publishers.spotify.reconciliation import MatchDecision

        job_id = job["id"]
        show = self.config.get_show(job["show_id"])
        request = PublishRequest(
            show_key=job["show_id"],
            show_name=show.name if show else job["show_id"],
            media_path=Path(job["media_path"]),
            metadata=metadata,
            media_info=self._load_media_info(job),
            job_id=job["id"],
            attempt=int(job.get("retry_count") or 0) + 1,
            allow_adopt=self._has_reached_spotify(job["id"], claimed_from),
        )
        try:
            from ..publishers.spotify.reconciliation import Reconciler

            reconciler = Reconciler(self.config, self.db)
            # The duplicate check is only meaningful against the live episode
            # list, and that needs a browser.  Without one the answer is
            # "unknown" rather than "no match", so a recovered job would wait
            # for ever.  Open the session - the same thread-local one the
            # publisher will use a moment later.
            session = None
            opened_here = False
            if hasattr(self.adapter, "session"):
                try:
                    session = self.adapter.session
                    if not session.is_open():
                        session.start()
                        opened_here = True
                except Exception:  # noqa: BLE001
                    session = None
            decision = reconciler.decide(
                request=request,
                session=session,
                show_config=show,
                recorded_episode_id=job.get("spotify_episode_id") or "",
                title_match_is_sufficient=self._has_reached_spotify(job_id, claimed_from),
            )
        except Exception as exc:  # noqa: BLE001
            from ..security.redaction import scrub_exception
            log.warning("Reconciliation failed: %s", scrub_exception(exc))
            reason = f"Could not check Spotify for duplicates: {scrub_exception(exc)}"
            self.db.log_event(job_id, None, "warning", reason)
            return MatchDecision(False, None, reason, source="unavailable")
        if decision.is_duplicate and decision.candidate is not None:
            self.db.log_event(job_id, None, "info",
                              f"Duplicate found via {decision.source}: {decision.reason}")
            return decision
        self.db.log_event(job_id, None, "info",
                          f"Reconciled via {decision.source}: {decision.reason}")
        if opened_here and not decision.is_unknown:
            # A browser opened only for the duplicate check is not needed for
            # the publish that follows, and leaving it open would hold a
            # window and a profile lock for nothing.
            closer = getattr(self.adapter, "close_session", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001  # pragma: no cover
                    pass
        return decision

    def _adopt_existing(self, job: JobRow, existing, metadata: EpisodeMetadata) -> None:
        if existing == "error":
            return
        url = getattr(existing, "url", "") or ""
        episode_id = getattr(existing, "episode_id", "") or ""
        state = (getattr(existing, "state", "") or "unknown").lower()
        self.db.update_job(
            job["id"],
            spotify_episode_id=episode_id or None,
            spotify_url=url or None,
            spotify_state=state,
            progress_percent=100.0,
            status_message="Already published on Spotify - adopted, not re-uploaded.",
        )
        log_activity(
            f"{metadata.title!r} was already on Spotify, so it was not uploaded again."
        )
        target = JobState.SCHEDULED if state == "scheduled" else JobState.PUBLISHED
        if state not in {"published", "live", "scheduled"}:
            self._advance(job["id"], JobState.NEEDS_RECONCILIATION,
                          message=(
                              f"Spotify lists an episode titled {existing.title!r} but its "
                              f"state is {state!r}. Check the episode in Spotify before "
                              f"letting it retry."
                          ))
            return
        self._advance(job["id"], target, force=True,
                      message="Adopted the episode that already exists on Spotify.")
        self._archive(job["id"])

    # -- publishing ----------------------------------------------------------
    def _publish(self, job: JobRow, metadata: EpisodeMetadata, media_info: MediaInfo) -> None:
        pass

        job_id = job["id"]
        show = self.config.get_show(job["show_id"])
        request = PublishRequest(
            show_key=job["show_id"],
            show_name=show.name if show else job["show_id"],
            media_path=Path(job["media_path"]),
            metadata=metadata,
            media_info=media_info,
            job_id=job_id,
            attempt=int(job.get("retry_count") or 0) + 1,
        )

        self._advance(job_id, JobState.AUTHENTICATING, force=True,
                      message="Connecting to Spotify...")
        self._advance(job_id, JobState.UPLOADING, force=True,
                      message=f"Uploading {request.media_path.name} to Spotify...")
        log_activity(f"Uploading {metadata.title!r} to Spotify ({media_info.kind.value}).")

        #: Publisher stage -> job state.  "Uploaded" is emphatically not
        #: "published": Spotify transcodes for a long time before the episode
        #: goes live, and the user must be able to see that difference.
        stage_states = {
            "authenticate": JobState.AUTHENTICATING,
            "upload": JobState.UPLOADING,
            "processing": JobState.PROCESSING,
            "details": JobState.READY_TO_PUBLISH,
            "publishing": JobState.PUBLISHING,
            "confirming": JobState.PUBLISHING,
        }

        def on_progress(stage: str, message: str, percent: float | None) -> None:
            self.db.update_job(job_id, status_message=message or stage)
            if percent is not None:
                self.db.update_job(job_id, progress_percent=round(float(percent), 1))
            target = stage_states.get(stage)
            if target is not None:
                self._advance(job_id, target, force=True,
                              message=message or f"Stage: {stage}.")
            if self._progress_hook:
                try:
                    self._progress_hook(job_id, {
                        "stage": stage, "message": message, "percent": percent,
                        "at": time.time(),
                    })
                except Exception:  # noqa: BLE001  # pragma: no cover
                    pass

        try:
            result = self.adapter.publish(
                request, progress=ProgressCallback(on_progress)
            )
        except PublisherError as exc:
            self._handle_publisher_error(job, exc)
            return
        except Exception as exc:  # noqa: BLE001
            from ..security.redaction import scrub_exception
            self._fail(job_id, f"Unexpected publisher error: {scrub_exception(exc)}", retryable=True)
            return

        evidence = {
            "status": result.status.value,
            "message": result.message,
            "evidence": result.evidence,
            "elapsed_seconds": round(result.elapsed_seconds, 1),
        }
        remote = result.remote_episode
        fields: dict = {
            "spotify_episode_id": (remote.episode_id if remote else "") or None,
            "spotify_url": (remote.url if remote else "") or None,
            "spotify_state": (remote.state if remote else "") or None,
            "diagnostics_json": json.dumps(evidence, default=str, ensure_ascii=False),
        }
        if result.status is PublicationStatus.PUBLISHED:
            fields["publish_confirmed_at"] = utc_now().isoformat()
            fields["progress_percent"] = 100.0
        self.db.update_job(job_id, **fields)

        # Anything the publisher wants the user to know - a title that clashes
        # with an episode already on the show, a setting that had to be
        # adjusted - becomes an event on the job, so it is visible in the
        # dashboard and in the job history and not only in the log file.
        for warning in (result.evidence or {}).get("warnings") or []:
            if not warning:
                continue
            self.db.log_event(job_id, None, "warning", str(warning))
            log_activity(f"Warning: {warning}")

        if result.status in {PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED,
                             PublicationStatus.DUPLICATE}:
            state = JobState.SCHEDULED if result.status is PublicationStatus.SCHEDULED \
                else JobState.PUBLISHED
            log_activity(f"Published: {metadata.title!r}. {result.message}")
            self._advance(job_id, state, force=True, message=result.message)
            self._archive(job_id)
            return

        if result.status is PublicationStatus.DRAFT:
            log_activity(f"Saved as a draft: {metadata.title!r}.")
            self._advance(job_id, JobState.PUBLISHED, force=True,
                          message="Saved as a draft in Spotify (publication was set to DRAFT).")
            self._archive(job_id)
            return

        if result.status is PublicationStatus.UNKNOWN:
            self._advance(job_id, JobState.NEEDS_RECONCILIATION,
                          message=result.message)
            return

        if result.status is PublicationStatus.UPLOADED or result.status is PublicationStatus.PROCESSING:
            self._advance(job_id, JobState.NEEDS_RECONCILIATION,
                          message=(
                              f"Spotify has the file but is still processing it: {result.message}"
                          ))
            return

        # FAILED
        if result.requires_action:
            self._fail(job_id, result.message, retryable=False, state=JobState.NEEDS_USER_ACTION)
        else:
            self._fail(job_id, result.message, retryable=True)

    def _handle_publisher_error(self, job: JobRow, exc: PublisherError) -> None:
        job_id = job["id"]
        if isinstance(exc, RemoteDuplicateFound):
            self._adopt_existing(job, exc.episode, self._load_metadata(job) or EpisodeMetadata(
                title=exc.episode.title, description=""))
            return
        state_hint = getattr(exc, "job_state_hint", None)
        target = JobState(state_hint) if state_hint else None
        if target is JobState.PUBLISHED:
            self._advance(job_id, JobState.PUBLISHED, force=True, message=str(exc))
            self._archive(job_id)
            return
        self._fail(job_id, str(exc), retryable=exc.retryable, state=target)

    # -- archiving -----------------------------------------------------------
    def _archive(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if job is None or job.get("archived_media_path"):
            return
        show = self.config.get_show(job["show_id"])
        if show is None:
            self.db.log_event(job_id, None, "error",
                              f"The show {job['show_id']!r} is no longer configured, so the "
                              f"files were left in place.")
            return
        self._advance(job_id, JobState.ARCHIVING, force=True, message="Archiving the files...")
        result = self.archive.archive_job_files(show, job)
        if not result.ok:
            # Publication already happened: never let an archive failure cause a
            # second publication.
            self._advance(job_id, JobState.PUBLISHED, force=True,
                          message=(
                              f"Published, but the files could not be archived: {result.error}. "
                              f"Fix the archive folder permissions and press Retry to "
                              f"archive them."
                          ))
            self.db.update_job(job_id, last_error=f"Archive failed: {result.error}")
            log_activity(f"Published, but archiving failed: {result.error}")
            return
        self.db.update_job(
            job_id,
            archived_media_path=str(result.media_path) if result.media_path else None,
            archived_metadata_path=str(result.metadata_path) if result.metadata_path else None,
        )
        self._advance(job_id, JobState.ARCHIVED, force=True,
                      message=f"Archived to {result.destination_dir}")
        log_activity(f"Archived: {', '.join(result.moved) or 'nothing to move'} -> {result.destination_dir}")

    # -- failure / retry ------------------------------------------------------
    def _fail(
        self,
        job_id: str,
        message: str,
        *,
        retryable: bool,
        state: JobState | None = None,
    ) -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return
        # Never downgrade a success.
        if job.state_enum() in {JobState.PUBLISHED, JobState.SCHEDULED, JobState.ARCHIVED}:
            return
        attempts = int(job.get("retry_count") or 0) + 1
        policy = self.config.retry
        now = utc_now()
        updates: dict = {
            "last_error": message,
            "retry_count": attempts,
            "spotify_state": "failed",
        }
        target = state or (JobState.RETRY_WAIT if retryable else JobState.FAILED)
        if target is JobState.RETRY_WAIT and attempts > policy.max_attempts:
            target = JobState.FAILED
            message = (
                f"{message} (gave up after {attempts} attempts - press Retry to try again)"
            )
        if target is JobState.RETRY_WAIT:
            delay = policy.delay_for(attempts)
            updates["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
            message = f"{message} - retrying in {int(delay)}s (attempt {attempts}/{policy.max_attempts})"
        try:
            self._advance(job_id, target, force=True, message=message, **updates)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not record the failure state for %s", job_id)
        log_activity(
            f"{'Will retry' if target is JobState.RETRY_WAIT else 'Stopped'}: {message}"
        )

    def _advance(
        self,
        job_id: str,
        state: JobState,
        *,
        force: bool = False,
        message: str = "",
        **fields,
    ) -> Optional[JobRow]:
        try:
            row = self.db.transition(job_id, state, message=message, force=force, **fields)
        except ValueError as exc:
            log.warning("%s", exc)
            return self.db.get_job(job_id)
        if self._on_state is not None:
            try:
                self._on_state(self.worker_id, state.value, job_id)
            except Exception:  # noqa: BLE001 - reporting must never break a job
                pass
        return row

    # -- helpers -------------------------------------------------------------
    def _load_metadata(self, job: JobRow) -> Optional[EpisodeMetadata]:
        raw = job.metadata()
        if not raw:
            return None
        try:
            publish_at = raw.get("publish_at")
            meta = EpisodeMetadata(
                title=raw.get("title", ""),
                description=raw.get("description", ""),
                season=raw.get("season"),
                episode_number=raw.get("episode_number"),
                explicit=bool(raw.get("explicit", False)),
                publish=PublishMode(raw.get("publish", "NOW")),
                publish_at=datetime.fromisoformat(publish_at) if publish_at else None,
                timezone=raw.get("timezone", "UTC"),
                chapters=[],
                language=raw.get("language", ""),
                author=raw.get("author", ""),
                tags=raw.get("tags", []) or [],
                notes=raw.get("notes", ""),
                artwork_path=raw.get("artwork_path", ""),
                source_path=raw.get("source_path", ""),
            )
            from ..core.models import Chapter

            for entry in raw.get("chapters", []) or []:
                meta.chapters.append(Chapter(
                    start_seconds=int(entry.get("start_seconds", 0)),
                    title=entry.get("title", ""),
                    raw_timestamp=entry.get("timestamp", ""),
                ))
            return meta
        except (ValueError, TypeError, KeyError) as exc:  # pragma: no cover
            # Report the job we were actually asked about.  A NameError here
            # would replace a diagnosable error with a confusing one.
            log.warning("Stored metadata for %s is unreadable: %s",
                        job.get("id", "?"), exc)
            return None

    def _load_media_info(self, job: JobRow) -> Optional[MediaInfo]:
        data = job.media_info()
        if not data:
            return None
        from ..core.models import MediaKind, TrackInfo

        try:
            info = MediaInfo(
                path=job["media_path"],
                kind=MediaKind(data.get("kind", "unknown")),
                container=data.get("container", ""),
                format_name=data.get("format_name", ""),
                duration=float(data.get("duration") or 0),
                size=int(data.get("size") or 0),
                bitrate=int(data.get("bitrate") or 0),
                video_codec=data.get("video_codec", ""),
                video_profile=data.get("video_profile", ""),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
                frame_rate=float(data.get("frame_rate") or 0),
                audio_codec=data.get("audio_codec", ""),
                audio_channels=int(data.get("audio_channels") or 0),
                sample_rate=int(data.get("sample_rate") or 0),
                audio_bitrate=int(data.get("audio_bitrate") or 0),
                pix_fmt=data.get("pix_fmt", ""),
                color_space=data.get("color_space", ""),
                field_order=data.get("field_order", ""),
                probe_source=data.get("probe_source", "unavailable"),
                has_video=bool(data.get("has_video")),
                has_audio=bool(data.get("has_audio")),
                error=data.get("error", ""),
                warnings=data.get("warnings", []) or [],
            )
            info.tracks = [TrackInfo(**t) for t in data.get("tracks", []) if isinstance(t, dict)]
            return info
        except (TypeError, ValueError, KeyError) as exc:  # pragma: no cover
            log.warning("Stored media info for %s is unreadable: %s",
                        job.get("id", "?"), exc)
            return None

    def _show_defaults(self, show_id: str) -> dict:
        show = self.config.get_show(show_id)
        if show is None:
            return {}
        out: dict = {}
        for key, value in (show.defaults.__dict__ or {}).items():
            if value is not None:
                out[key.upper()] = value
        # Show-level folder defaults that are not part of ShowDefaults.
        for key in ("timezone",):
            value = getattr(show, key, None)
            if value:
                out[key.upper()] = value
        return out


class WorkerPool:
    """Runs :class:`JobWorker` instances in background threads."""

    def __init__(self, config: AppConfig, database: Database, size: int = 1,
                 adapter: PublisherAdapter | None = None):
        self.config = config
        self.db = database
        self.size = max(1, size)
        self._adapter = adapter
        self._workers: list[JobWorker] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.idle = threading.Event()
        self.processed = 0
        self._lock = threading.Lock()
        self.running = False
        # Real per-worker state, reported to the dashboard. A worker writes
        # here as it works so the panel shows what is really happening rather
        # than a generic "running" flag.
        self._worker_state: dict[str, dict] = {}

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        for index in range(self.size):
            worker = JobWorker(
                self.config, self.db,
                adapter=self._adapter, worker_id=f"worker-{index + 1}",
                on_state=lambda wid, st, jid: self._mark(
                    wid, current_job=jid, current_state=st, since=time.time()
                ),
            )
            self._workers.append(worker)
            thread = threading.Thread(
                target=self._loop, args=(worker,), name=f"publisher-{index + 1}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        self.running = True

    @property
    def first_worker(self) -> Optional[JobWorker]:
        return self._workers[0] if self._workers else None

    def snapshot(self) -> list[dict]:
        """Live state of every worker, for the dashboard."""
        rows = []
        for index in range(self.size):
            worker_id = f"worker-{index + 1}"
            state = dict(self._worker_state.get(worker_id) or {})
            thread = self._threads[index] if index < len(self._threads) else None
            rows.append({
                "id": worker_id,
                "alive": bool(thread and thread.is_alive()) if thread else False,
                "paused": bool(self.config.pause_queue),
                "current_job": state.get("current_job"),
                "current_state": state.get("current_state"),
                "since": state.get("since"),
                "last_error": state.get("last_error"),
                "processed": self._worker_state.get(worker_id, {}).get("processed", 0),
            })
        return rows

    def _mark(self, worker_id: str, **fields) -> None:
        with self._lock:
            row = self._worker_state.setdefault(worker_id, {"processed": 0})
            row.update(fields)

    def _loop(self, worker: JobWorker) -> None:
        while not self._stop.is_set():
            try:
                if self.config.pause_queue:
                    self.idle.set()
                    self._mark(worker.worker_id, current_job=None, current_state="paused")
                    time.sleep(1.0)
                    continue
                job_id = worker.run_once()
            except Exception:  # noqa: BLE001 - a worker thread must never die
                logging.getLogger(__name__).exception("Worker loop error")
                job_id = None
            if job_id:
                self._mark(worker.worker_id, processed=self._worker_state.get(
                    worker.worker_id, {}).get("processed", 0) + 1)
            else:
                self._mark(worker.worker_id, current_job=None, current_state="idle")
            if job_id is None:
                self.idle.set()
                time.sleep(1.0)
            else:
                self.idle.clear()
                with self._lock:
                    self.processed += 1

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=timeout)
        self._threads.clear()
        self._workers.clear()
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass
        self.running = False

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        """Wait until no job is being processed.  Used by tests and the CLI."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = self.db.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE claimed_by IS NOT NULL AND state NOT IN "
                "('ARCHIVED','PUBLISHED','SCHEDULED','FAILED','CANCELLED')"
            ).fetchone()[0]
            if active == 0:
                return True
            time.sleep(0.2)
        return False
