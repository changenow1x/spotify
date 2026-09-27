"""Map the canonical metadata model onto Spotify's own fields.

This is the *only* place that knows how Spotify spells things.  A Spotify
change is a change to this file plus ``selectors.py`` - never to the metadata
parser, the database or the queue.

Field support, transcribed from Spotify for Creators documentation:

============================  ==========================================
Canonical field               Where it goes on Spotify
============================  ==========================================
``title``                     "Details > Title"
``description``               "Details > Description" (rich text)
``chapters``                  Appended to the description as
                              ``(MM:SS) Title`` lines.  This is
                              Spotify's documented chapter mechanism.
``explicit``                  "Content checks > Explicit"
``season`` / ``episode_number``  "Additional details > Season / Episode"
``publish`` + ``publish_at``  "Publish date > Now | Schedule"
``language``                  not settable per episode; recorded locally
``author`` / ``notes`` /      not supported per episode; recorded locally
``tags`` / ``artwork_path``   recorded locally, never invented into the UI
============================  ==========================================

Nothing is invented: a field Spotify does not expose is kept locally and
reported, not smuggled into a field that means something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ...core.models import Chapter, EpisodeMetadata, PublishMode, format_in_timezone
from .selectors import SELECTORS

__all__ = [
    "SpotifyEpisodePayload",
    "map_metadata_to_spotify",
    "build_description",
    "CHANNELS_HEADER",
    "SPOTIFY_DESCRIPTION_LIMIT",
]

#: Spotify's description field is generous; this is a safety ceiling for the
#: editor so an enormous file cannot hang the browser.
SPOTIFY_DESCRIPTION_LIMIT = 10_000

CHANNELS_HEADER = "Chapters"


def build_description(meta: EpisodeMetadata, *, append_chapters: bool = True) -> str:
    """Compose the text that goes into Spotify's description field.

    The user's description is used **verbatim**.  Chapters are appended in
    Spotify's documented ``(MM:SS) Title`` format under a short header, which
    is what makes them clickable in the Spotify player.

    The chapter titles themselves are never modified - Spotify strips leading
    index prefixes like "1 - " on its side, so we do not pre-empt it.
    """
    body = (meta.description or "").strip()
    if not append_chapters or not meta.chapters:
        return body[:SPOTIFY_DESCRIPTION_LIMIT]

    lines: list[str] = [body] if body else []
    if lines:
        lines.append("")
    lines.append(CHANNELS_HEADER)
    lines.append("")
    lines.extend(chapter.to_description_line() for chapter in meta.chapters)
    return "\n".join(lines)[:SPOTIFY_DESCRIPTION_LIMIT]


@dataclass
class SpotifyEpisodePayload:
    """Exactly what the publisher will type into Spotify's UI."""

    title: str
    description: str
    explicit: bool
    season: Optional[int] = None
    episode_number: Optional[int] = None
    publish_now: bool = True
    schedule_date: str = ""          # YYYY-MM-DD, Spotify's input format
    schedule_time: str = ""          # HH:MM, Spotify's input format
    schedule_local_label: str = ""   # for logs and the dashboard
    schedule_utc: Optional[datetime] = None
    chapters: list[Chapter] = field(default_factory=list)
    #: Fields that exist locally but Spotify has no per-episode control for.
    local_only: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: The logical selector set each value will be written into.
    targets: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "description_preview": (
                self.description[:400] + ("..." if len(self.description) > 400 else "")
            ),
            "description_length": len(self.description),
            "explicit": self.explicit,
            "season": self.season,
            "episode_number": self.episode_number,
            "publish_now": self.publish_now,
            "schedule_date": self.schedule_date,
            "schedule_time": self.schedule_time,
            "schedule_local": self.schedule_local_label,
            "schedule_utc": self.schedule_utc.isoformat() if self.schedule_utc else None,
            "chapter_count": len(self.chapters),
            "chapters": [c.as_dict() for c in self.chapters],
            "local_only": self.local_only,
            "warnings": self.warnings,
        }

    def describe(self) -> str:
        when = "now" if self.publish_now else f"scheduled for {self.schedule_local_label}"
        bits = [f'title={self.title!r}', when]
        if self.season is not None:
            bits.append(f"season={self.season}")
        if self.episode_number is not None:
            bits.append(f"episode={self.episode_number}")
        bits.append(f"explicit={self.explicit}")
        if self.chapters:
            bits.append(f"chapters={len(self.chapters)}")
        return ", ".join(bits)


def map_metadata_to_spotify(
    meta: EpisodeMetadata,
    *,
    include_chapters: bool = True,
) -> SpotifyEpisodePayload:
    """Translate the canonical model into Spotify's field-by-field values."""
    payload = SpotifyEpisodePayload(
        title=meta.title.strip(),  # surrounding blanks are never meaningful
        description=build_description(meta, append_chapters=include_chapters),
        explicit=bool(meta.explicit),
        season=meta.season,
        episode_number=meta.episode_number,
        chapters=list(meta.chapters) if include_chapters else [],
    )

    if meta.publish is PublishMode.SCHEDULE and meta.publish_at is not None:
        payload.publish_now = False
        local = meta.publish_at.astimezone(_tz(meta.timezone))
        payload.schedule_date = local.strftime("%Y-%m-%d")
        payload.schedule_time = local.strftime("%H:%M")
        payload.schedule_utc = meta.publish_at
        payload.schedule_local_label = format_in_timezone(meta.publish_at, meta.timezone)
    else:
        payload.publish_now = True

    # Locally recorded, not sent to Spotify.
    payload.local_only = {
        "author": meta.author,
        "language": meta.language,
        "tags": meta.tags,
        "notes": meta.notes,
        "artwork_path": meta.artwork_path,
        "source_file": meta.source_path,
    }
    if payload.title != meta.title:
        payload.warnings.append(
            "Blank characters at the start and end of TITLE were removed before "
            "uploading; the wording itself was not changed."
        )
    for key in ("author", "language", "artwork_path"):
        if payload.local_only.get(key):
            payload.warnings.append(
                f"{key.upper()} has no per-episode control in Spotify for Creators; "
                f"it is stored with the job but not uploaded."
            )

    payload.targets = {
        "title": "title_input",
        "description": "description_input",
        "explicit": "explicit_toggle",
        "season": "season_input",
        "episode_number": "episode_number_input",
        "publish_now": "publish_now_radio",
        "publish_schedule": "publish_schedule_radio",
        "schedule_date": "schedule_date_input",
        "schedule_time": "schedule_time_input",
        "publish_button": "publish_button",
    }
    return payload


def _tz(name: str):
    from ...core.models import resolve_timezone
    return resolve_timezone(name)


def check_payload_supported(payload: SpotifyEpisodePayload) -> list[str]:
    """Return blocking problems for a payload.  Used before opening a browser."""
    problems: list[str] = []
    if not payload.title:
        problems.append("The episode title is empty.")
    if not payload.description.strip():
        problems.append("The episode description is empty.")
    if not payload.publish_now and not (payload.schedule_date and payload.schedule_time):
        problems.append(
            "The episode is scheduled but the date or time could not be determined."
        )
    return problems


def supported_summary() -> dict:
    """What this adapter can do, for the dashboard and the docs."""
    from ..base import AdapterCapabilities

    caps = AdapterCapabilities(
        audio_episodes=True,
        video_episodes=True,
        immediate_publish=True,
        scheduled_publish=True,
        season_episode_numbers=True,
        explicit_flag=True,
        description=True,
        chapters=True,
        chapters_via_description=True,
        episode_artwork=False,
        read_existing_episodes=True,
        delete_episodes=False,
    )
    return {
        "capabilities": caps.as_dict(),
        "selectors": {name: SELECTORS[name].primary() for name in sorted(SELECTORS)},
    }
