"""Explicit, persisted job state machine.

Every transition is validated against :data:`TRANSITIONS`.  A job that is found
in a non-terminal state after a crash is *recovered* by :func:`recover_state`,
which decides - deterministically - what the job should resume as.  That is the
single source of truth for crash recovery, so the watcher, the worker and the
dashboard can never disagree about what a job means.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class JobState(str, Enum):
    DISCOVERED = "DISCOVERED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    AUTHENTICATING = "AUTHENTICATING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    SCHEDULED = "SCHEDULED"
    ARCHIVING = "ARCHIVING"
    ARCHIVED = "ARCHIVED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    CANCELLED = "CANCELLED"


#: States that mean "we have already created the episode on Spotify" and must
#: therefore never blindly re-create it.
STATES_AFTER_REMOTE_CREATE: FrozenSet[JobState] = frozenset(
    {JobState.UPLOADING, JobState.PROCESSING, JobState.READY_TO_PUBLISH,
     JobState.PUBLISHING, JobState.PUBLISHED, JobState.SCHEDULED,
     JobState.ARCHIVING, JobState.ARCHIVED, JobState.NEEDS_RECONCILIATION}
)

#: States that still need a worker to pick them up.
ACTIVE_STATES: FrozenSet[JobState] = frozenset(
    {JobState.DISCOVERED, JobState.VALIDATING, JobState.VALIDATED,
     JobState.QUEUED, JobState.AUTHENTICATING, JobState.UPLOADING,
     JobState.PROCESSING, JobState.READY_TO_PUBLISH, JobState.PUBLISHING,
     JobState.ARCHIVING, JobState.RETRY_WAIT}
)

#: States the app will never touch again without explicit user action.
TERMINAL_STATES: FrozenSet[JobState] = frozenset(
    {JobState.PUBLISHED, JobState.SCHEDULED, JobState.ARCHIVED,
     JobState.FAILED, JobState.CANCELLED}
)

#: States that mean an episode is live (or will be) on Spotify.
SUCCESS_STATES: FrozenSet[JobState] = frozenset(
    {JobState.PUBLISHED, JobState.SCHEDULED, JobState.ARCHIVED}
)

#: States a job can be retried from (after reconciliation).
RETRYABLE_STATES: FrozenSet[JobState] = frozenset(
    {JobState.FAILED, JobState.RETRY_WAIT, JobState.NEEDS_USER_ACTION,
     JobState.NEEDS_RECONCILIATION, JobState.DISCOVERED, JobState.QUEUED,
     JobState.VALIDATED}
)

TRANSITIONS: dict[JobState, FrozenSet[JobState]] = {
    JobState.DISCOVERED: frozenset(
        {JobState.VALIDATING, JobState.FAILED, JobState.CANCELLED,
         JobState.RETRY_WAIT, JobState.NEEDS_USER_ACTION}
    ),
    JobState.VALIDATING: frozenset(
        {JobState.VALIDATED, JobState.FAILED, JobState.CANCELLED,
         JobState.RETRY_WAIT, JobState.NEEDS_USER_ACTION}
    ),
    JobState.VALIDATED: frozenset(
        {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED,
         JobState.RETRY_WAIT, JobState.VALIDATING}
    ),
    JobState.QUEUED: frozenset(
        {JobState.AUTHENTICATING, JobState.FAILED, JobState.CANCELLED,
         JobState.RETRY_WAIT, JobState.NEEDS_RECONCILIATION, JobState.VALIDATING}
    ),
    JobState.AUTHENTICATING: frozenset(
        {JobState.UPLOADING, JobState.RETRY_WAIT, JobState.FAILED,
         JobState.NEEDS_USER_ACTION, JobState.CANCELLED,
         JobState.NEEDS_RECONCILIATION}
    ),
    JobState.UPLOADING: frozenset(
        {JobState.PROCESSING, JobState.RETRY_WAIT, JobState.FAILED,
         JobState.NEEDS_USER_ACTION, JobState.CANCELLED,
         JobState.NEEDS_RECONCILIATION}
    ),
    JobState.PROCESSING: frozenset(
        {JobState.READY_TO_PUBLISH, JobState.RETRY_WAIT, JobState.FAILED,
         JobState.NEEDS_USER_ACTION, JobState.CANCELLED,
         JobState.NEEDS_RECONCILIATION}
    ),
    JobState.READY_TO_PUBLISH: frozenset(
        {JobState.PUBLISHING, JobState.RETRY_WAIT, JobState.FAILED,
         JobState.NEEDS_USER_ACTION, JobState.CANCELLED,
         JobState.NEEDS_RECONCILIATION, JobState.PROCESSING}
    ),
    JobState.PUBLISHING: frozenset(
        {JobState.PUBLISHED, JobState.SCHEDULED, JobState.RETRY_WAIT,
         JobState.FAILED, JobState.NEEDS_USER_ACTION, JobState.CANCELLED,
         JobState.NEEDS_RECONCILIATION, JobState.ARCHIVING}
    ),
    JobState.PUBLISHED: frozenset(
        {JobState.ARCHIVING, JobState.FAILED, JobState.NEEDS_RECONCILIATION}
    ),
    JobState.SCHEDULED: frozenset(
        {JobState.ARCHIVING, JobState.PUBLISHED, JobState.FAILED,
         JobState.NEEDS_RECONCILIATION}
    ),
    JobState.ARCHIVING: frozenset(
        {JobState.ARCHIVED, JobState.FAILED, JobState.RETRY_WAIT}
    ),
    # ARCHIVED is terminal.  A failure to archive leaves the job in PUBLISHED.
    JobState.ARCHIVED: frozenset(),
    JobState.RETRY_WAIT: frozenset(
        {JobState.AUTHENTICATING, JobState.QUEUED, JobState.VALIDATING,
         JobState.UPLOADING, JobState.PROCESSING, JobState.READY_TO_PUBLISH,
         JobState.PUBLISHING, JobState.NEEDS_RECONCILIATION, JobState.FAILED,
         JobState.CANCELLED, JobState.NEEDS_USER_ACTION, JobState.ARCHIVING,
         JobState.PUBLISHED}
    ),
    JobState.FAILED: frozenset(
        {JobState.RETRY_WAIT, JobState.QUEUED, JobState.VALIDATING,
         JobState.NEEDS_RECONCILIATION, JobState.CANCELLED, JobState.ARCHIVING,
         JobState.PUBLISHED, JobState.UPLOADING, JobState.PROCESSING,
         JobState.READY_TO_PUBLISH, JobState.PUBLISHING, JobState.AUTHENTICATING,
         JobState.NEEDS_USER_ACTION}
    ),
    JobState.NEEDS_USER_ACTION: frozenset(
        {JobState.RETRY_WAIT, JobState.FAILED, JobState.CANCELLED,
         JobState.QUEUED, JobState.AUTHENTICATING, JobState.UPLOADING,
         JobState.PROCESSING, JobState.READY_TO_PUBLISH, JobState.PUBLISHING,
         JobState.NEEDS_RECONCILIATION, JobState.PUBLISHED, JobState.ARCHIVING}
    ),
    JobState.NEEDS_RECONCILIATION: frozenset(
        {JobState.RETRY_WAIT, JobState.PUBLISHED, JobState.SCHEDULED,
         JobState.ARCHIVING, JobState.ARCHIVED, JobState.QUEUED,
         JobState.FAILED, JobState.CANCELLED, JobState.NEEDS_USER_ACTION,
         JobState.READY_TO_PUBLISH, JobState.UPLOADING, JobState.PROCESSING,
         JobState.PUBLISHING, JobState.AUTHENTICATING}
    ),
    JobState.CANCELLED: frozenset({JobState.QUEUED, JobState.RETRY_WAIT}),
}


class IllegalTransition(RuntimeError):
    def __init__(self, src: JobState | str, dst: JobState | str):
        self.src = JobState(src) if not isinstance(src, JobState) else src
        self.dst = JobState(dst) if not isinstance(dst, JobState) else dst
        super().__init__(
            f"Illegal state transition {self.src.value} -> {self.dst.value}"
        )

    def as_dict(self) -> dict:
        return {"error": "illegal_transition", "from": self.src.value, "to": self.dst.value}


def can_transition(src: JobState | str, dst: JobState | str) -> bool:
    s = JobState(src)
    d = JobState(dst)
    return d in TRANSITIONS.get(s, frozenset())


def validate_transition(src: JobState | str, dst: JobState | str) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def is_terminal(state: JobState | str) -> bool:
    return JobState(state) in TERMINAL_STATES


def is_active(state: JobState | str) -> bool:
    return JobState(state) in ACTIVE_STATES


def is_success(state: JobState | str) -> bool:
    return JobState(state) in SUCCESS_STATES


def needs_remote_check(state: JobState | str) -> bool:
    """True when we cannot be sure whether Spotify already has this episode."""
    return JobState(state) in STATES_AFTER_REMOTE_CREATE


def recover_state(state: JobState | str) -> JobState:
    """Map a persisted (possibly stale) state to its post-crash resume state.

    This is the crash-recovery decision table.  It is deliberately
    conservative: anything past the point where a remote episode may exist is
    routed to ``NEEDS_RECONCILIATION`` so the worker asks Spotify what is
    really there instead of guessing.
    """
    s = JobState(state)
    if s in TERMINAL_STATES:
        return s
    # These are already "safe to resume" states, so recovery must leave them
    # exactly as they are.  Without this the second restart would push every
    # job that was already waiting for a duplicate check into "needs your
    # attention", and the user would have to intervene on each restart.
    if s in {JobState.NEEDS_RECONCILIATION, JobState.NEEDS_USER_ACTION}:
        return s
    # Mid-remote-create states: we may have already created the episode.
    if s in {JobState.UPLOADING, JobState.PROCESSING, JobState.READY_TO_PUBLISH,
             JobState.PUBLISHING, JobState.ARCHIVING, JobState.AUTHENTICATING}:
        return JobState.NEEDS_RECONCILIATION
    if s in {JobState.DISCOVERED, JobState.VALIDATING, JobState.VALIDATED,
             JobState.QUEUED, JobState.RETRY_WAIT}:
        return JobState.RETRY_WAIT
    return JobState.NEEDS_USER_ACTION  # pragma: no cover - defensive


#: Human-readable, non-technical wording used in the dashboard and CLI.
STATE_LABELS: dict[JobState, str] = {
    JobState.DISCOVERED: "Detected",
    JobState.VALIDATING: "Validating",
    JobState.VALIDATED: "Validated",
    JobState.QUEUED: "Waiting in queue",
    JobState.AUTHENTICATING: "Connecting to Spotify",
    JobState.UPLOADING: "Uploading to Spotify",
    JobState.PROCESSING: "Spotify is processing",
    JobState.READY_TO_PUBLISH: "Ready to publish",
    JobState.PUBLISHING: "Publishing",
    JobState.PUBLISHED: "Published",
    JobState.SCHEDULED: "Scheduled",
    JobState.ARCHIVING: "Archiving",
    JobState.ARCHIVED: "Archived",
    JobState.RETRY_WAIT: "Waiting to retry",
    JobState.FAILED: "Failed",
    JobState.NEEDS_USER_ACTION: "Needs your attention",
    JobState.NEEDS_RECONCILIATION: "Checking Spotify for duplicates",
    JobState.CANCELLED: "Cancelled",
}

#: Explains *why* a state was reached, for the dashboard "what does this mean" hint.
STATE_HINTS: dict[JobState, str] = {
    JobState.UPLOADING: "The file is being sent to Spotify. Do not close the program.",
    JobState.PROCESSING: "Spotify received the file and is transcoding it. No action needed.",
    JobState.READY_TO_PUBLISH: "Spotify accepted the media. Metadata is being written.",
    JobState.PUBLISHING: "Confirming publication with Spotify.",
    JobState.PUBLISHED: "Spotify confirmed the episode is live.",
    JobState.SCHEDULED: "Spotify confirmed the episode and will publish it at the scheduled time.",
    JobState.NEEDS_RECONCILIATION: "The app restarted mid-upload and is checking Spotify so the episode is not published twice.",
    JobState.NEEDS_USER_ACTION: "The app needs you to do something - see the error below.",
    JobState.RETRY_WAIT: "A temporary problem occurred; the app will try again automatically.",
}
