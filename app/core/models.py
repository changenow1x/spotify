"""Canonical domain models.

Layering (deliberately kept separate so Spotify changes never touch the parser):

``EpisodeMetadata``  <- canonical, platform-neutral model
       ^
       |  metadata/parser.py parses the user's .txt into this
       |
``SpotifyEpisodePayload`` <- publishers/spotify/mapping.py maps the canonical
       |                        model onto exactly what Spotify's UI exposes
``MediaInfo``        <- media/inspector.py, independent of any platform
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "PublishMode",
    "Chapter",
    "EpisodeMetadata",
    "MediaInfo",
    "TrackInfo",
    "MediaKind",
    "ValidationIssue",
    "ValidationReport",
    "utc_now",
    "utc_now_iso",
    "to_utc",
    "from_local_datetime",
    "resolve_timezone",
    "format_in_timezone",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def resolve_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, raising a clear error if unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(
            f"Unknown timezone {name!r}. Use an IANA name such as 'Europe/Berlin'."
        ) from exc


def from_local_datetime(naive: datetime, tz_name: str) -> datetime:
    """Convert a naive local wall-clock datetime in *tz_name* to aware UTC.

    DST gaps/overlaps are handled explicitly rather than silently: a time that
    does not exist (spring-forward gap) is rejected, and an ambiguous time
    (autumn overlap) resolves to the *earlier* offset, which is the standard
    "first occurrence" rule.
    """
    tz = resolve_timezone(tz_name)
    candidate = naive.replace(tzinfo=tz)
    # Detect a non-existent local time (spring forward): round-tripping changes it.
    round_trip = candidate.astimezone(timezone.utc).astimezone(tz)
    if round_trip.replace(tzinfo=None) != naive:
        raise ValueError(
            f"{naive.isoformat()} does not exist in timezone {tz_name!r} "
            f"(daylight-saving gap). Choose another time."
        )
    return candidate.astimezone(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("Refusing to treat a naive datetime as UTC")
    return dt.astimezone(timezone.utc)


def format_in_timezone(dt: datetime, tz_name: str, *, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(resolve_timezone(tz_name)).strftime(fmt)


class PublishMode(str, Enum):
    NOW = "NOW"
    SCHEDULE = "SCHEDULE"
    DRAFT = "DRAFT"


class MediaKind(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Chapter:
    """One chapter marker.  ``start_seconds`` is the canonical value."""

    start_seconds: int
    title: str
    raw_timestamp: str

    @property
    def timestamp(self) -> str:
        """Render as ``MM:SS`` (or ``HH:MM:SS`` past an hour)."""
        h, rem = divmod(self.start_seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def to_description_line(self) -> str:
        """The exact format Spotify documents for description chapters.

        Spotify accepts ``(MM:SS) Title`` / ``MM:SS Title`` etc. in the episode
        description.  We emit the unambiguous parenthesised form.
        """
        return f"({self.timestamp}) {self.title}"

    def as_dict(self) -> dict:
        return {
            "start_seconds": self.start_seconds,
            "title": self.title,
            "timestamp": self.timestamp,
            "raw_timestamp": self.raw_timestamp,
        }


@dataclass
class EpisodeMetadata:
    """Canonical, platform-neutral episode metadata.

    ``description`` holds the user's text exactly as written (minus the
    structured fields).  Chapters are kept separately: the Spotify mapping
    layer decides how to render them, so adding another platform never requires
    re-parsing the user's file.
    """

    title: str
    description: str
    season: Optional[int] = None
    episode_number: Optional[int] = None
    explicit: bool = False
    publish: PublishMode = PublishMode.NOW
    publish_at: Optional[datetime] = None          # always timezone-aware UTC
    timezone: str = "UTC"                           # IANA name the user wrote
    publish_date_raw: str = ""
    publish_time_raw: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    author: str = ""
    show: str = ""
    language: str = ""
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    artwork_path: str = ""
    raw_text: str = ""
    source_path: str = ""
    warnings: list[str] = field(default_factory=list)
    #: Fields that were explicitly present in the file (vs. defaulted).
    explicit_fields: set[str] = field(default_factory=set)

    @property
    def is_scheduled(self) -> bool:
        return self.publish is PublishMode.SCHEDULE and self.publish_at is not None

    def local_publish_label(self) -> str:
        if self.publish_at is None:
            return "-"
        return format_in_timezone(self.publish_at, self.timezone)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["publish"] = self.publish.value
        d["publish_at"] = self.publish_at.isoformat() if self.publish_at else None
        d["chapters"] = [c.as_dict() for c in self.chapters]
        d["explicit_fields"] = sorted(self.explicit_fields)
        return d


@dataclass
class TrackInfo:
    index: int
    kind: str                 # "video" | "audio" | "subtitle" | "data" | ...
    codec: str = ""
    profile: str = ""
    language: str = ""
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    channels: int = 0
    sample_rate: int = 0
    bitrate: int = 0
    duration: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MediaInfo:
    """Everything we could learn about a media file, from any source."""

    path: str
    kind: MediaKind = MediaKind.UNKNOWN
    container: str = ""
    format_name: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    video_codec: str = ""
    video_profile: str = ""
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    audio_codec: str = ""
    audio_channels: int = 0
    sample_rate: int = 0
    audio_bitrate: int = 0
    pix_fmt: str = ""
    color_space: str = ""
    field_order: str = ""
    tracks: list[TrackInfo] = field(default_factory=list)
    #: Which tool produced this: "ffprobe" | "builtin" | "unavailable"
    probe_source: str = "unavailable"
    has_video: bool = False
    has_audio: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def resolution_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "-"

    def video_track_count(self) -> int:
        return sum(1 for t in self.tracks if t.kind == "video")

    def audio_track_count(self) -> int:
        return sum(1 for t in self.tracks if t.kind == "audio")

    def aspect_ratio(self) -> float:
        if self.width and self.height:
            return self.width / self.height
        return 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["duration_hms"] = format_duration(self.duration)
        d["tracks"] = [t.as_dict() for t in self.tracks]
        return d


def format_duration(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "-"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class ValidationIssue:
    """A single validation problem.

    ``field`` is ``None`` for file-level problems.  ``code`` is a stable
    machine-readable identifier so tests and the dashboard can rely on it.
    """

    code: str
    message: str
    field: Optional[str] = None
    severity: str = "error"          # "error" | "warning"
    hint: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    def add_error(self, code: str, message: str, *, field_: str | None = None, hint: str = "") -> None:
        self.errors.append(ValidationIssue(code, message, field_, "error", hint))
        self.ok = False

    def add_warning(self, code: str, message: str, *, field_: str | None = None, hint: str = "") -> None:
        self.warnings.append(ValidationIssue(code, message, field_, "warning", hint))

    def error_codes(self) -> list[str]:
        return [e.code for e in self.errors]

    def warning_codes(self) -> list[str]:
        return [w.code for w in self.warnings]

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [e.as_dict() for e in self.errors],
            "warnings": [w.as_dict() for w in self.warnings],
            "info": self.info,
        }

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "OK"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} error(s): " + "; ".join(e.message for e in self.errors))
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s): " + "; ".join(w.message for w in self.warnings))
        return " | ".join(parts)
