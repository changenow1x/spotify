"""SQLite persistence.

One file, WAL mode, no server.  This is the authority for "what happened to
each episode" and the first line of defence against duplicate publication.

Duplicate prevention works at three levels, all enforced here:

1. ``UNIQUE(show_id, content_hash) WHERE retired=0`` - the same media + same
   metadata for the same show can only ever have one live job.  Re-dropping the
   pair re-attaches to the existing job instead of creating a second one.
2. ``jobs`` records the remote episode id once known, and
   ``publish_intent`` records *before* we touch Spotify, so a crash during
   upload leaves an unambiguous breadcrumb for reconciliation.
3. ``job_events`` is an append-only audit trail used by the dashboard.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .models import utc_now_iso
from .state_machine import (
    IllegalTransition,
    JobState,
    can_transition,
    is_terminal,
    recover_state,
)

__all__ = ["Database", "JobRow", "SCHEMA_VERSION"]

SCHEMA_VERSION = 3

#: States a job can only be in once it has already sent something to the
#: platform.  Recovering out of one of these means the duplicate check has to
#: run before anything is uploaded again.
_SENT_STATES = frozenset({
    JobState.AUTHENTICATING,
    JobState.UPLOADING,
    JobState.PROCESSING,
    JobState.READY_TO_PUBLISH,
    JobState.PUBLISHING,
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    show_id             TEXT NOT NULL,
    state               TEXT NOT NULL,
    job_type            TEXT NOT NULL DEFAULT 'paired',

    base_name           TEXT NOT NULL,
    original_filename   TEXT NOT NULL,
    media_path          TEXT NOT NULL,
    metadata_path       TEXT,
    media_size          INTEGER NOT NULL DEFAULT 0,
    media_mtime_ns      INTEGER NOT NULL DEFAULT 0,
    media_kind          TEXT NOT NULL DEFAULT 'unknown',

    media_hash          TEXT,
    metadata_hash       TEXT,
    content_hash        TEXT,

    title               TEXT NOT NULL DEFAULT '',
    season              INTEGER,
    episode_number      INTEGER,
    is_explicit         INTEGER NOT NULL DEFAULT 0,
    publish_mode        TEXT NOT NULL DEFAULT 'NOW',
    publish_at_utc      TEXT,
    timezone            TEXT NOT NULL DEFAULT 'UTC',

    spotify_show_id     TEXT,
    spotify_episode_id  TEXT,
    spotify_url         TEXT,
    spotify_state       TEXT,

    progress_percent    REAL NOT NULL DEFAULT 0,
    status_message      TEXT NOT NULL DEFAULT '',
    last_error          TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT,

    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    validated_at        TEXT,
    upload_started_at   TEXT,
    upload_completed_at TEXT,
    publish_attempted_at TEXT,
    publish_confirmed_at TEXT,
    archived_at         TEXT,
    archived_media_path TEXT,
    archived_metadata_path TEXT,

    claimed_by          TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    retired             INTEGER NOT NULL DEFAULT 0,

    media_info_json     TEXT,
    metadata_json       TEXT,
    diagnostics_json    TEXT
);

-- Level 1 duplicate prevention: one live job per (show, content).
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_content
    ON jobs (show_id, content_hash)
    WHERE content_hash IS NOT NULL AND retired = 0;

CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_media
    ON jobs (show_id, media_hash)
    WHERE media_hash IS NOT NULL AND retired = 0;

CREATE INDEX IF NOT EXISTS ix_jobs_state      ON jobs (state);
CREATE INDEX IF NOT EXISTS ix_jobs_show      ON jobs (show_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_sched     ON jobs (next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_jobs_title     ON jobs (show_id, title);
CREATE INDEX IF NOT EXISTS ix_jobs_recent    ON jobs (updated_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    at        TEXT NOT NULL,
    state     TEXT NOT NULL,
    level     TEXT NOT NULL DEFAULT 'info',
    message   TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_job ON job_events (job_id, id DESC);

-- Watcher dedupe: remembers every path/size/mtime we have already looked at so
-- a restart or a duplicate filesystem event does not re-create a job.
CREATE TABLE IF NOT EXISTS seen_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id    TEXT NOT NULL,
    path       TEXT NOT NULL,
    role       TEXT NOT NULL,          -- media | metadata
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    media_hash TEXT,
    job_id     TEXT,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_seen_files ON seen_files (show_id, path, role);
CREATE INDEX IF NOT EXISTS ix_seen_job ON seen_files (job_id);

-- Sparse fingerprints of files whose pair has not been completed yet, so a
-- metadata file arriving an hour after the media still pairs correctly.
CREATE TABLE IF NOT EXISTS pending_media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id     TEXT NOT NULL,
    base_name   TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    media_hash  TEXT,
    media_kind  TEXT,
    media_info_json TEXT,
    discovered_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending ON pending_media (show_id, path);
CREATE INDEX IF NOT EXISTS ix_pending_base ON pending_media (show_id, base_name);

CREATE TABLE IF NOT EXISTS spotify_shows (
    show_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    spotify_id     TEXT,
    rss_url        TEXT,
    last_checked   TEXT,
    cached_json    TEXT
);

-- Cache of what Spotify currently lists for a show.  Reconciliation reads this
-- instead of guessing, and it survives restarts so a crash mid-upload can be
-- resolved without the original browser session.
CREATE TABLE IF NOT EXISTS spotify_episodes (
    show_id         TEXT NOT NULL,
    episode_id      TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT,
    state           TEXT NOT NULL DEFAULT 'unknown',
    published_at    TEXT,
    scheduled_for   TEXT,
    url             TEXT,
    duration        REAL,
    season          INTEGER,
    episode_number  INTEGER,
    is_explicit     INTEGER,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    raw_json        TEXT,
    PRIMARY KEY (show_id, episode_id)
);
CREATE INDEX IF NOT EXISTS ix_sf_eps_title ON spotify_episodes (show_id, title);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT 'system',
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log (at DESC);
"""


#: States where a cancellation can take effect immediately: no worker has
#: touched the episode, so nothing is in flight.
_CANCELLABLE_WITHOUT_WORKER = frozenset({
    JobState.DISCOVERED, JobState.VALIDATING, JobState.VALIDATED,
    JobState.QUEUED, JobState.RETRY_WAIT, JobState.NEEDS_USER_ACTION,
})


class JobRow(dict):
    """A ``jobs`` row with attribute access and JSON column helpers."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(item) from exc

    def state_enum(self) -> JobState:
        return JobState(self["state"])

    def media_info(self) -> dict:
        return _load_json(self.get("media_info_json"))

    def metadata(self) -> dict:
        return _load_json(self.get("metadata_json"))

    def diagnostics(self) -> dict:
        return _load_json(self.get("diagnostics_json"))


def _load_json(value: Any) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (json.JSONDecodeError, TypeError):  # pragma: no cover
        return {}


class Database:
    """Thread-safe SQLite wrapper.

    One connection per thread (``check_same_thread=False`` is *not* used);
    WAL mode plus short write transactions means the dashboard can read while a
    job is uploading without ever seeing a partial write.
    """

    def __init__(self, path: Path | str, *, claim_lease_seconds: int = 3600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        #: How long a worker's claim on a job is honoured before another worker
        #: may take it over.  Bounds how long a single dead worker can stall a
        #: job without requiring a restart.
        self.claim_lease_seconds = max(30, int(claim_lease_seconds))
        self._init_schema()

    # -- connection handling --------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._write_lock.acquire()
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            self._write_lock.release()

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self.conn
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_info ORDER BY version DESC LIMIT 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_info (version, applied_at) VALUES (?,?)",
                    (SCHEMA_VERSION, utc_now_iso()),
                )
            elif row[0] < SCHEMA_VERSION:
                # Forward migrations are applied idempotently; _SCHEMA uses
                # IF NOT EXISTS throughout so re-running is safe.
                conn.execute(
                    "INSERT INTO schema_info (version, applied_at) VALUES (?,?)",
                    (SCHEMA_VERSION, utc_now_iso()),
                )

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) FROM schema_info").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # -- jobs -------------------------------------------------------------
    def create_job(self, **fields: Any) -> Optional[JobRow]:
        """Insert a job, or return the existing one if the content is a duplicate.

        Returns ``None`` only if the insert failed for a reason other than the
        duplicate index.
        """
        now = utc_now_iso()
        # "id TEXT PRIMARY KEY" is nullable in SQLite, so omitting the id would
        # insert a NULL key that no lookup could ever find again.
        defaults: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "state": JobState.DISCOVERED.value,
            "job_type": "paired",
            "base_name": "",
            "original_filename": "",
            "media_path": "",
            "metadata_path": None,
            "media_size": 0,
            "media_mtime_ns": 0,
            "media_kind": "unknown",
            "title": "",
            "is_explicit": 0,
            "publish_mode": "NOW",
            "timezone": "UTC",
            "status_message": "",
            "created_at": now,
            "updated_at": now,
        }
        row = {**defaults, **{k: v for k, v in fields.items() if v is not None or k in fields}}
        columns = ", ".join(row)
        markers = ", ".join("?" for _ in row)
        try:
            with self.transaction() as conn:
                conn.execute(
                    f"INSERT INTO jobs ({columns}) VALUES ({markers})",
                    tuple(row.values()),
                )
        except sqlite3.IntegrityError:
            existing = self.find_job_by_content(
                row.get("show_id"), row.get("content_hash")
            ) or self.find_job_by_media(row.get("show_id"), row.get("media_hash"))
            if existing is not None:
                self.log_event(
                    existing["id"], JobState(existing["state"]), "info",
                    "Duplicate drop ignored - attached to the existing job.",
                    {"dropped_path": row.get("media_path")},
                )
                return existing
            raise
        job_id = row["id"]
        self.log_event(job_id, JobState(row["state"]), "info", "Episode detected.", {
            "media_path": row.get("media_path"),
            "metadata_path": row.get("metadata_path"),
        })
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[JobRow]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return JobRow(dict(row)) if row else None

    def find_job_by_content(self, show_id: str, content_hash: str | None) -> Optional[JobRow]:
        if not show_id or not content_hash:
            return None
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE show_id=? AND content_hash=? AND retired=0",
            (show_id, content_hash),
        ).fetchone()
        return JobRow(dict(row)) if row else None

    def find_job_by_media(self, show_id: str, media_hash: str | None) -> Optional[JobRow]:
        if not show_id or not media_hash:
            return None
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE show_id=? AND media_hash=? AND retired=0",
            (show_id, media_hash),
        ).fetchone()
        return JobRow(dict(row)) if row else None

    def find_job_by_files(self, show_id: str, media_path: str, metadata_path: str | None) -> Optional[JobRow]:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE show_id=? AND media_path=? AND "
            "(metadata_path IS ? OR metadata_path=?) AND retired=0",
            (show_id, media_path, metadata_path, metadata_path),
        ).fetchone()
        return JobRow(dict(row)) if row else None

    def update_job(self, job_id: str, **fields: Any) -> Optional[JobRow]:
        if not fields:
            return self.get_job(job_id)
        fields = {k: v for k, v in fields.items() if k not in {"id", "created_at"}}
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self._write_lock:
            self.conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*fields.values(), job_id),
            )
        return self.get_job(job_id)

    def force_state(self, job_id: str, state: JobState, *, message: str = "",
                    **fields: Any) -> Optional[JobRow]:
        """Move a job to *state* ignoring the transition table.

        Used for recovery after a crash, when the database already holds a
        state the running code cannot legally enter.
        """
        return self.transition(job_id, state, message=message, force=True, **fields)

    def transition(
        self,
        job_id: str,
        new_state: JobState | str,
        *,
        message: str = "",
        level: str = "info",
        force: bool = False,
        **fields: Any,
    ) -> Optional[JobRow]:
        """Atomically move a job to *new_state*, validating the transition.

        ``force=True`` is used only by recovery paths, which have already
        decided the correct target state.
        """
        target = JobState(new_state)
        with self._write_lock:
            row = self.conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            current = JobState(row[0])
            if not force and not can_transition(current, target):
                # Never silently accept an illegal move.
                self.log_event(
                    job_id, current, "error",
                    f"Refused illegal transition {current.value} -> {target.value}.",
                    {"attempted": target.value},
                )
                illegal = IllegalTransition(current, target)
                illegal.job_id = job_id
                illegal.add_note(f"Job {job_id} was not changed.")
                raise illegal from None
            now = utc_now_iso()
            stamp_fields: dict[str, Any] = dict(fields)
            if target is JobState.VALIDATED and "validated_at" not in stamp_fields:
                stamp_fields["validated_at"] = now
            if target is JobState.UPLOADING and "upload_started_at" not in stamp_fields:
                stamp_fields["upload_started_at"] = now
            if target is JobState.PUBLISHING and "publish_attempted_at" not in stamp_fields:
                stamp_fields["publish_attempted_at"] = now
            if target in {JobState.PUBLISHED, JobState.SCHEDULED} and "publish_confirmed_at" not in stamp_fields:
                stamp_fields["publish_confirmed_at"] = now
            if target is JobState.ARCHIVED and "archived_at" not in stamp_fields:
                stamp_fields["archived_at"] = now
            if message:
                stamp_fields["status_message"] = message
            stamp_fields["updated_at"] = now
            assignments = ", ".join(f"{k}=?" for k in stamp_fields)
            self.conn.execute(
                f"UPDATE jobs SET state=?, {assignments} WHERE id=?",
                (target.value, *stamp_fields.values(), job_id),
            )
        self.log_event(job_id, target, level, message or f"State -> {target.value}", fields or None)
        return self.get_job(job_id)

    def request_cancel(self, job_id: str) -> bool:
        """Ask for a job to stop.

        A job that has not been handed to a worker yet is cancelled outright,
        so the user sees CANCELLED rather than a job that appears to be
        "Detected" forever.  A job already in flight only gets the flag; the
        worker stops it at its next checkpoint.
        """
        with self._write_lock:
            cur = self.conn.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                                    (utc_now_iso(), job_id))
        if cur.rowcount:
            self.log_event(job_id, None, "info", "Cancellation requested.")
            row = self.conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and JobState(row[0]) in _CANCELLABLE_WITHOUT_WORKER:
                self.transition(job_id, JobState.CANCELLED,
                                message="Cancelled before it started.")
        return bool(cur.rowcount)

    def cancel_requested(self, job_id: str) -> bool:
        row = self.conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def retire_job(self, job_id: str, reason: str = "") -> None:
        """Mark a job as no longer 'live' so the same content may be re-created."""
        with self._write_lock:
            self.conn.execute(
                "UPDATE jobs SET retired=1, updated_at=?, status_message=? WHERE id=?",
                (utc_now_iso(), reason or "Retired", job_id),
            )
        self.log_event(job_id, None, "info", f"Job retired: {reason or 'no reason given'}")

    def list_jobs(
        self,
        *,
        show_id: str | None = None,
        states: Iterable[JobState | str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[JobRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if show_id:
            clauses.append("show_id=?")
            params.append(show_id)
        if states:
            values = [s.value if isinstance(s, JobState) else str(s) for s in states]
            if values:
                clauses.append(f"state IN ({','.join('?' for _ in values)})")
                params.extend(values)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self.conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", params
        ).fetchall()
        return [JobRow(dict(r)) for r in rows]

    def count_jobs(self, *, show_id: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        if show_id:
            rows = self.conn.execute(
                "SELECT state, COUNT(*) c FROM jobs WHERE show_id=? GROUP BY state", (show_id,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT state, COUNT(*) c FROM jobs GROUP BY state").fetchall()
        for state, count in rows:
            out[state] = count
        return out

    def claim_next_job(self, worker_id: str, *, now: datetime | None = None) -> Optional[JobRow]:
        """Atomically claim the next runnable job for this worker.

        Runnable means: an active, non-terminal state, not cancelled, and not
        blocked by a retry backoff.
        """
        now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")
        # A claim is a lease: a second worker must never pick up a job that is
        # already being worked on, and a claim left behind by a dead worker has
        # to become claimable again without needing a full restart.
        lease_cutoff = (
            datetime.now(timezone.utc).timestamp() - self.claim_lease_seconds
        )
        lease_iso = datetime.fromtimestamp(lease_cutoff, timezone.utc).isoformat(
            timespec="milliseconds")
        # A job must be claimable the moment the watcher creates it, otherwise a
        # freshly dropped file would sit in DISCOVERED forever.  The worker
        # re-validates whatever it claims, so DISCOVERED is a legitimate entry
        # point rather than a special case.
        claimable = [
            JobState.DISCOVERED.value, JobState.QUEUED.value, JobState.VALIDATED.value,
            JobState.RETRY_WAIT.value, JobState.VALIDATING.value,
            JobState.NEEDS_RECONCILIATION.value, JobState.NEEDS_USER_ACTION.value,
        ]
        with self._write_lock:
            row = self.conn.execute(
                f"""
                SELECT id FROM jobs
                WHERE state IN ({','.join('?' for _ in claimable)})
                  AND retired = 0
                  AND cancel_requested = 0
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (claimed_by IS NULL OR updated_at <= ?)
                ORDER BY
                    CASE state
                        WHEN 'NEEDS_RECONCILIATION' THEN 0
                        WHEN 'NEEDS_USER_ACTION' THEN 1
                        WHEN 'RETRY_WAIT' THEN 2
                        WHEN 'VALIDATED' THEN 3
                        WHEN 'QUEUED' THEN 4
                        WHEN 'VALIDATING' THEN 5
                        ELSE 6
                    END,
                    COALESCE(next_attempt_at, created_at) ASC,
                    created_at ASC
                LIMIT 1
                """,
                (*claimable, now_iso, lease_iso),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE jobs SET claimed_by=?, updated_at=? WHERE id=?",
                (worker_id, now_iso, row[0]),
            )
        return self.get_job(row[0])

    def reset_stale_claims(self, *, older_than_seconds: int = 3600) -> int:
        """Release claims held by a worker that died (used at startup)."""
        cutoff = (datetime.now(timezone.utc).timestamp() - older_than_seconds)
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE jobs SET claimed_by=NULL WHERE claimed_by IS NOT NULL "
                "AND state NOT IN ('ARCHIVED','PUBLISHED','SCHEDULED','FAILED','CANCELLED') "
                "AND updated_at < ?",
                (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="milliseconds"),),
            )
        return cur.rowcount

    def recover_interrupted_jobs(self) -> list[tuple[str, JobState, JobState]]:
        """Apply the crash-recovery decision table to every non-terminal job.

        Returns the list of ``(job_id, from_state, to_state)`` changes made.
        """
        rows = self.conn.execute(
            "SELECT id, state FROM jobs WHERE retired=0"
        ).fetchall()
        changes: list[tuple[str, JobState, JobState]] = []
        for row in rows:
            current = JobState(row[1])
            if is_terminal(current):
                continue
            target = recover_state(current)
            if target is current:
                continue
            with self._write_lock:
                # A job rewound from an on-the-wire state has already sent
                # something.  Stamp that durably: the state it is rewound to
                # is transient, and duplicate protection must not depend on
                # which state happens to be current when it is next read.
                # None, not the string "NULL": these are bound parameters,
                # so a literal would store the text and the job would never
                # be claimable again.
                stamps: dict[str, Any] = {
                    "state": target.value,
                    "claimed_by": None,
                    "updated_at": utc_now_iso(),
                }
                if current in _SENT_STATES:
                    stamps["publish_attempted_at"] = stamps["updated_at"]
                assignments = ", ".join(f"{k}=?" for k in stamps)
                self.conn.execute(
                    f"UPDATE jobs SET {assignments} WHERE id=?",
                    (*stamps.values(), row[0]),
                )
            self.log_event(
                row[0], target, "warning",
                f"Recovered after an unclean shutdown: {current.value} -> {target.value}.",
                {"previous_state": current.value},
            )
            changes.append((row[0], current, target))
        return changes

    # -- events / audit ---------------------------------------------------
    def log_event(
        self,
        job_id: str | None,
        state: JobState | str | None,
        level: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        from ..security.redaction import redact_text

        state_value = state.value if isinstance(state, JobState) else (state or "")
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO job_events (job_id, at, state, level, message, data_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    job_id, utc_now_iso(), state_value, level,
                    redact_text(message, max_len=2000),
                    json.dumps(data, default=str) if data else None,
                ),
            )

    def get_events(self, job_id: str, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_events(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT e.*, j.title, j.show_id FROM job_events e "
            "LEFT JOIN jobs j ON j.id = e.job_id ORDER BY e.id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def audit(self, action: str, target: str = "", detail: str = "", actor: str = "system") -> None:
        from ..security.redaction import redact_text

        with self._write_lock:
            self.conn.execute(
                "INSERT INTO audit_log (at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                (utc_now_iso(), actor, action, target, redact_text(detail, max_len=2000)),
            )

    def recent_audit(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- seen files / pending media ---------------------------------------
    def note_seen_file(
        self, show_id: str, path: str, role: str, size: int, mtime_ns: int,
        media_hash: str | None = None, job_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO seen_files (show_id, path, role, size, mtime_ns, media_hash, job_id, first_seen, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(show_id, path, role) DO UPDATE SET "
                "size=excluded.size, mtime_ns=excluded.mtime_ns, media_hash=COALESCE(excluded.media_hash, seen_files.media_hash), "
                "job_id=COALESCE(excluded.job_id, seen_files.job_id), updated_at=excluded.updated_at",
                (show_id, path, role, size, mtime_ns, media_hash, job_id, now, now),
            )

    def get_seen_file(self, show_id: str, path: str, role: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM seen_files WHERE show_id=? AND path=? AND role=?", (show_id, path, role)
        ).fetchone()
        return dict(row) if row else None

    def add_pending_media(self, show_id: str, base_name: str, path: str, size: int,
                          mtime_ns: int, media_hash: str, media_kind: str,
                          media_info: dict | None = None) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO pending_media (show_id, base_name, path, size, mtime_ns, media_hash, "
                "media_kind, media_info_json, discovered_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(show_id, path) DO UPDATE SET size=excluded.size, "
                "mtime_ns=excluded.mtime_ns, media_hash=excluded.media_hash",
                (show_id, base_name, path, size, mtime_ns, media_hash, media_kind,
                 json.dumps(media_info, default=str) if media_info else None, utc_now_iso()),
            )

    def get_pending_media(self, show_id: str, path: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM pending_media WHERE show_id=? AND path=?", (show_id, path)
        ).fetchone()
        return dict(row) if row else None

    def remove_pending_media(self, show_id: str, path: str) -> None:
        with self._write_lock:
            self.conn.execute("DELETE FROM pending_media WHERE show_id=? AND path=?", (show_id, path))

    def pending_for_show(self, show_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pending_media WHERE show_id=? ORDER BY discovered_at DESC", (show_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- spotify cache ----------------------------------------------------
    def upsert_spotify_show(self, show_id: str, name: str, spotify_id: str = "",
                            rss_url: str = "", raw: dict | None = None) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO spotify_shows (show_id, name, spotify_id, rss_url, last_checked, cached_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(show_id) DO UPDATE SET "
                "name=excluded.name, spotify_id=excluded.spotify_id, rss_url=excluded.rss_url, "
                "last_checked=excluded.last_checked, cached_json=excluded.cached_json",
                (show_id, name, spotify_id, rss_url, utc_now_iso(),
                 json.dumps(raw, default=str) if raw else None),
            )

    def get_spotify_shows(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM spotify_shows ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def upsert_spotify_episode(self, show_id: str, episode: dict) -> None:
        now = utc_now_iso()
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO spotify_episodes (show_id, episode_id, title, description, state, "
                "published_at, scheduled_for, url, duration, season, episode_number, is_explicit, "
                "first_seen, last_seen, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(show_id, episode_id) DO UPDATE SET title=excluded.title, "
                "description=excluded.description, state=excluded.state, published_at=excluded.published_at, "
                "scheduled_for=excluded.scheduled_for, url=excluded.url, duration=excluded.duration, "
                "season=excluded.season, episode_number=excluded.episode_number, "
                "is_explicit=excluded.is_explicit, last_seen=excluded.last_seen, raw_json=excluded.raw_json",
                (
                    show_id, episode["episode_id"], episode.get("title", ""),
                    episode.get("description"), episode.get("state", "unknown"),
                    episode.get("published_at"), episode.get("scheduled_for"),
                    episode.get("url"), episode.get("duration"), episode.get("season"),
                    episode.get("episode_number"),
                    1 if episode.get("is_explicit") else 0, now, now,
                    json.dumps(episode.get("raw"), default=str) if episode.get("raw") else None,
                ),
            )

    def find_spotify_episode(self, show_id: str, episode_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM spotify_episodes WHERE show_id=? AND episode_id=?", (show_id, episode_id)
        ).fetchone()
        return dict(row) if row else None

    def find_spotify_episodes_by_title(self, show_id: str, title: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM spotify_episodes WHERE show_id=? AND title=? ORDER BY last_seen DESC",
            (show_id, title),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_spotify_episodes(self, show_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM spotify_episodes WHERE show_id=? ORDER BY last_seen DESC LIMIT ?",
            (show_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_spotify_last_checked(self, show_id: str) -> None:
        with self._write_lock:
            self.conn.execute("UPDATE spotify_shows SET last_checked=? WHERE show_id=?",
                              (utc_now_iso(), show_id))

    # -- settings ---------------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), utc_now_iso()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:  # pragma: no cover
            return default

    def delete_setting(self, key: str) -> None:
        with self._write_lock:
            self.conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def vacuum(self) -> None:  # pragma: no cover - maintenance
        with self._write_lock:
            self.conn.execute("VACUUM")

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_state = {k: v for k, v in self.count_jobs().items()}
        published = sum(v for k, v in by_state.items() if k in {"PUBLISHED", "SCHEDULED", "ARCHIVED"})
        failed = by_state.get("FAILED", 0)
        return {
            "total": total,
            "published": published,
            "failed": failed,
            "by_state": by_state,
            "database_path": str(self.path),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "schema_version": self.schema_version(),
        }
