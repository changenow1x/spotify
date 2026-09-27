"""The platform-neutral publisher interface.

Concretely this is what every adapter must be able to do:

``authenticate``      establish a legitimate session (never by bypassing
                      security controls - the human logs in, we keep the session)
``validate_configuration``  fail fast if this installation cannot publish here
``publish``           create the episode and return a *verified* result
``reconcile``         answer "does this episode already exist remotely?" -
                      the duplicate-prevention backbone
``get_status``        cheap, local-only status for the dashboard
``close``             release the session

The contract that matters most is in :meth:`PublisherAdapter.publish`: it must
return a :class:`PublishResult` whose ``status`` distinguishes *uploaded*,
*processing*, *published*, *scheduled* and *unknown*.  Returning
``PUBLISHED`` means the remote side confirmed it, not that a button was clicked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

__all__ = [
    "PublicationStatus",
    "AuthStatus",
    "AdapterCapabilities",
    "PublishRequest",
    "PublishResult",
    "HealthCheck",
    "ProgressCallback",
    "PublisherAdapter",
    "PublisherError",
    "AuthenticationRequired",
    "ConfigurationError",
    "ValidationFailed",
    "UploadFailed",
    "RemoteDuplicateFound",
    "NeedsReconciliation",
]


class PublicationStatus(str, Enum):
    """The remote outcome.  Deliberately finer-grained than success/failure."""

    PUBLISHED = "PUBLISHED"            # confirmed live on the platform
    SCHEDULED = "SCHEDULED"            # confirmed scheduled for a future time
    UPLOADED = "UPLOADED"              # media accepted, still processing
    PROCESSING = "PROCESSING"          # platform is transcoding
    DRAFT = "DRAFT"                    # saved but deliberately not published
    DUPLICATE = "DUPLICATE"            # already existed; adopted, not re-created
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"                # we genuinely do not know -> reconcile

    @property
    def is_success(self) -> bool:
        return self in {
            PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED,
            PublicationStatus.DUPLICATE,
        }


class AuthStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    NOT_CONNECTED = "NOT_CONNECTED"
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    NEEDS_HUMAN = "NEEDS_HUMAN"        # user must act in a visible browser


@dataclass(frozen=True)
class AdapterCapabilities:
    """What this adapter can actually do on this platform today."""

    audio_episodes: bool = False
    video_episodes: bool = False
    immediate_publish: bool = False
    scheduled_publish: bool = False
    season_episode_numbers: bool = False
    explicit_flag: bool = False
    description: bool = False
    chapters: bool = False
    #: Chapters delivered as timestamps in the description (Spotify's mechanism).
    chapters_via_description: bool = False
    episode_artwork: bool = False
    read_existing_episodes: bool = False
    delete_episodes: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ProgressCallback:
    """Called during a long operation so the dashboard can show real progress.

    ``percent`` is ``None`` when the platform does not expose progress - the
    dashboard then shows an indeterminate bar rather than inventing numbers.
    """

    _fn: Any
    stage: str = ""
    percent: Optional[float] = None
    message: str = ""

    def __call__(self, stage: str, message: str = "", percent: Optional[float] = None) -> None:
        if self._fn is None:
            return
        try:
            self._fn(stage, message, percent)
        except Exception:  # noqa: BLE001 - progress must never break a job
            pass


@dataclass
class PublishRequest:
    """Everything an adapter needs, in platform-neutral terms."""

    show_key: str
    show_name: str
    media_path: Path
    metadata: Any                      # app.core.models.EpisodeMetadata
    media_info: Any = None             # app.core.models.MediaInfo
    job_id: str = ""
    #: Bumped on every retry; adapters must treat it as an attempt counter.
    attempt: int = 1
    #: Populated by reconcile() before publish() is called.
    known_duplicate: Optional["RemoteEpisode"] = None
    #: True only when the caller has durable evidence that this job already
    #: reached the platform - an upload start, a publish attempt, a recorded
    #: remote id, or a recovery.  A same-titled remote episode is only proof
    #: of "this is the same episode" when this is set; otherwise it is a
    #: different recording and adopting it would lose the new file.
    allow_adopt: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def is_audio(self) -> bool:
        from ..core.models import MediaKind
        if self.media_info is None:
            return self.media_path.suffix.lower() in {".mp3", ".m4a", ".wav"}
        return self.media_info.kind is MediaKind.AUDIO


@dataclass
class RemoteEpisode:
    """A snapshot of an episode that exists on the platform."""

    episode_id: str
    title: str = ""
    state: str = "unknown"
    url: str = ""
    published_at: Optional[datetime] = None
    scheduled_for: Optional[datetime] = None
    duration: float = 0.0
    season: Optional[int] = None
    episode_number: Optional[int] = None
    is_explicit: bool = False
    description: str = ""
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "title": self.title,
            "state": self.state,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "duration": self.duration,
            "season": self.season,
            "episode_number": self.episode_number,
            "is_explicit": self.is_explicit,
        }


@dataclass
class PublishResult:
    status: PublicationStatus
    message: str = ""
    remote_episode: Optional[RemoteEpisode] = None
    #: Extra evidence that the result is trustworthy (ids, urls, DOM markers).
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Set when something needs the human (login, MFA, verification).
    requires_action: str = ""
    elapsed_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status.is_success

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "message": self.message,
            "remote_episode": self.remote_episode.as_dict() if self.remote_episode else None,
            "evidence": self.evidence,
            "requires_action": self.requires_action,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }


@dataclass
class HealthCheck:
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "details": self.details,
            "problems": self.problems,
        }


# -- exceptions -------------------------------------------------------------

class PublisherError(RuntimeError):
    """Base class for adapter failures.  Never contains a secret."""

    retryable: bool = True
    job_state_hint: str = "RETRY_WAIT"

    def as_dict(self) -> dict:
        return {
            "type": type(self).__name__,
            "message": str(self),
            "retryable": self.retryable,
            "job_state_hint": self.job_state_hint,
        }


class AuthenticationRequired(PublisherError):
    retryable = False
    job_state_hint = "NEEDS_USER_ACTION"


class NeedsVerification(PublisherError):
    retryable = False
    job_state_hint = "NEEDS_USER_ACTION"


class ConfigurationError(PublisherError):
    retryable = False
    job_state_hint = "FAILED"


class ValidationFailed(PublisherError):
    retryable = False
    job_state_hint = "FAILED"


class UploadFailed(PublisherError):
    retryable = True
    job_state_hint = "RETRY_WAIT"


class SelectorChanged(PublisherError):
    """The web interface no longer matches our selectors.

    This is the loud failure the specification demands: the app must stop and
    tell the user exactly what changed, never carry on as if it worked.
    """

    retryable = False
    job_state_hint = "NEEDS_USER_ACTION"

    def __init__(self, field_name: str, selectors: list[str], page: str = "", snapshot: str = ""):
        self.field_name = field_name
        self.selectors = selectors
        self.page = page
        self.snapshot = snapshot
        super().__init__(
            f"Could not find the {field_name!r} control on Spotify's page. "
            f"Spotify's website appears to have changed. "
            f"Tried {len(selectors)} selector(s): {selectors[0] if selectors else '(none)'!r}. "
            f"A page snapshot was saved so this can be fixed quickly."
        )


class RemoteDuplicateFound(PublisherError):
    retryable = False
    job_state_hint = "PUBLISHED"

    def __init__(self, episode: RemoteEpisode, how: str = ""):
        self.episode = episode
        self.how = how
        super().__init__(
            f"This episode already exists on Spotify as "
            f"{episode.title!r} ({episode.state}). It was adopted instead of "
            f"being uploaded again."
        )


class NeedsReconciliation(PublisherError):
    retryable = True
    job_state_hint = "NEEDS_RECONCILIATION"

    def __init__(self, message: str = "The remote state is not known yet."):
        super().__init__(message)


# -- adapter interface -------------------------------------------------------

@runtime_checkable
class PublisherAdapter(Protocol):
    """The contract every platform implements."""

    name: str
    display_name: str

    def capabilities(self) -> AdapterCapabilities:
        """What this adapter supports on this platform *today*."""

    def authenticate(self, *, interactive: bool = False) -> AuthStatus:
        """Establish a legitimate session."""

    def validate_configuration(self) -> HealthCheck:
        """Fail fast if this installation cannot publish to this platform."""

    def publish(self, request: PublishRequest, *, progress: ProgressCallback | None = None) -> PublishResult:
        """Create the episode and return a *verified* result."""

    def reconcile(self, show_key: str, request: PublishRequest) -> list[RemoteEpisode]:
        """List remote episodes that could be this episode."""

    def get_status(self) -> HealthCheck:
        """Cheap local status for the dashboard (must not require the network)."""

    def close(self) -> None:
        """Release the session and any browser resources."""
