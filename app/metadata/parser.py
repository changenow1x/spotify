"""The user's ``.txt`` metadata file in, a canonical model out.

This package is deliberately platform-neutral.  Nothing here knows what
Spotify calls anything; that translation lives in
``publishers/spotify/mapping.py``.  Adding a second platform therefore never
requires touching this parser.

Format
------
UTF-8 (BOM tolerated, CRLF or LF).  ``KEY=VALUE``.  A key with an empty value
starts a multiline block: every following non-blank line that is not itself a
``KEY=VALUE`` pair is appended to that key's value.

    TITLE=THOT - Der Schluessel zum Bewusstsein
    DESCRIPTION=Erste Zeile.
    Zweite Zeile bleibt Teil der Beschreibung.
    SEASON=1
    EPISODE=4
    EXPLICIT=false
    PUBLISH=SCHEDULE
    PUBLISH_DATE=2026-09-30
    PUBLISH_TIME=18:00
    TIMEZONE=Europe/Berlin
    CHAPTERS=
    00:00 Intro
    02:15 Das vergessene Wissen

Anything else is rejected with a line number and a plain-English explanation.
The parser never invents, truncates, or silently rewrites what the user typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.models import (
    Chapter,
    EpisodeMetadata,
    PublishMode,
    from_local_datetime,
    resolve_timezone,
)
from ..security.paths import MAX_FILENAME_LEN  # noqa: F401  (re-exported limit)
from .chapters import ChapterLineError, parse_chapters

__all__ = [
    "MetadataParseError",
    "ParsedFile",
    "parse_metadata_text",
    "parse_metadata_file",
    "KNOWN_FIELDS",
    "DERIVED_FIELDS",
]


class MetadataParseError(ValueError):
    """A syntax/structure problem in the metadata file.

    Carries a machine-readable ``code`` and a 1-based ``line_no`` so the
    dashboard can link straight to the offending line.
    """

    def __init__(self, message: str, *, line_no: int | None = None,
                 field_name: str | None = None, code: str = "metadata_syntax"):
        self.line_no = line_no
        self.field_name = field_name
        self.code = code
        location = f" (line {line_no})" if line_no else ""
        super().__init__(f"{message}{location}")


#: Fields the user may write directly.  ``DEFAULT_*`` is handled separately.
KNOWN_FIELDS: dict[str, str] = {
    "TITLE": "Episode title (required).",
    "DESCRIPTION": "Episode description / show notes (required).",
    "SEASON": "Season number, e.g. 1.",
    "EPISODE": "Episode number within the season, e.g. 4.",
    "EXPLICIT": "true/false - marks the episode as explicit.",
    "PUBLISH": "NOW, SCHEDULE or DRAFT.",
    "PUBLISH_DATE": "YYYY-MM-DD (required when PUBLISH=SCHEDULE).",
    "PUBLISH_TIME": "HH:MM or HH:MM:SS (required when PUBLISH=SCHEDULE).",
    "TIMEZONE": "IANA timezone, e.g. Europe/Berlin (default: show default).",
    "CHAPTERS": "Multiline chapter list: '00:00 Title' per line.",
    "AUTHOR": "Optional author/host shown in the show notes.",
    "SHOW": "Optional show name (must match the configured show).",
    "LANGUAGE": "Optional language tag, e.g. de or en-US.",
    "TAGS": "Optional comma-separated keywords.",
    "NOTES": "Optional private note; never sent to Spotify.",
    "ARTWORK": "Optional path to episode artwork (reported, not uploaded).",
}

#: Show-level defaults that a single episode may override.
DERIVED_FIELDS: dict[str, str] = {
    "DEFAULT_EXPLICIT": "Fallback EXPLICIT value for this show.",
    "DEFAULT_PUBLISH": "Fallback PUBLISH value for this show.",
    "DEFAULT_TIMEZONE": "Fallback TIMEZONE for this show.",
    "DEFAULT_LANGUAGE": "Fallback LANGUAGE for this show.",
    "DEFAULT_AUTHOR": "Fallback AUTHOR for this show.",
    "DEFAULT_SEASON": "Fallback SEASON for this show.",
    "DEFAULT_PUBLISH_TIME": "Fallback PUBLISH_TIME for this show.",
    "DEFAULT_PUBLISH_DATE": "Fallback PUBLISH_DATE for this show.",
    "DEFAULT_NOTES": "Fallback NOTES for this show.",
    "DEFAULT_TAGS": "Fallback TAGS for this show.",
}

_ALL_FIELDS = {**KNOWN_FIELDS, **DERIVED_FIELDS}

#: Only these fields may be continued on the following lines.  A stray line
#: after ``TITLE=`` is a typo, not a title continuation, and is rejected rather
#: than silently folded into the value.
MULTILINE_KEYS = {"DESCRIPTION", "CHAPTERS", "NOTES", "ARTWORK", "TAGS", "AUTHOR", "SHOW"}

_KEY_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=(?P<value>.*)$")
_TRUE = {"true", "yes", "y", "1", "on", "ja", "wahr"}
_FALSE = {"false", "no", "n", "0", "off", "nein", "falsch"}
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


@dataclass
class ParsedFile:
    """Raw result of parsing - a key/value map plus any structural problems."""

    values: dict[str, str] = field(default_factory=dict)
    #: field name -> 1-based line number where it was defined
    line_numbers: dict[str, int] = field(default_factory=dict)
    unknown: dict[str, tuple[int, str]] = field(default_factory=dict)
    raw_text: str = ""
    source_path: str = ""

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key.upper(), default)

    def has(self, key: str) -> bool:
        return key.upper() in self.values

    def get_derived(self, key: str) -> str:
        return self.values.get(f"DEFAULT_{key.upper()}", "")


def _decode(raw: bytes, path: Path) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_metadata_text(
    text: str,
    *,
    source_path: str = "",
    max_length: int = 200_000,
) -> ParsedFile:
    """Parse the ``KEY=VALUE`` text into a :class:`ParsedFile`.

    Raises :class:`MetadataParseError` only for *structural* problems (a
    continuation line with no key above it, or a file that is too large).
    Unknown keys are collected into ``unknown`` so the caller can decide
    whether to reject them; the validator rejects them by default.
    """
    if len(text) > max_length:
        raise MetadataParseError(
            f"The metadata file is {len(text)} characters long; the limit is {max_length}.",
            code="metadata_too_large",
        )

    parsed = ParsedFile(raw_text=text, source_path=source_path)
    current_key: str | None = None
    for line_no, raw_line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        if not raw_line.strip():
            # Blank line: ends a multiline block but is otherwise ignored.
            if current_key is not None and parsed.values.get(current_key, "").strip():
                parsed.values[current_key] += "\n"
            continue
        match = _KEY_RE.match(raw_line)
        if match:
            key = match.group("key").upper()
            value = match.group("value")
            if key in parsed.values:
                parsed.values[key] += "\n" + value
            else:
                parsed.values[key] = value
                parsed.line_numbers[key] = line_no
            current_key = key
            if key not in _ALL_FIELDS:
                parsed.unknown[key] = (line_no, value)
        else:
            if current_key is None:
                raise MetadataParseError(
                    f"This line is not a KEY=VALUE pair and no key came before it. "
                    f"Every line must look like TITLE=... or belong to a "
                    f"multiline block such as DESCRIPTION= or CHAPTERS=.",
                    line_no=line_no, code="orphan_line",
                )
            if current_key not in MULTILINE_KEYS:
                raise MetadataParseError(
                    f"{current_key}= does not take more lines, so {raw_line.strip()!r} "
                    f"cannot belong to it. Give the value on the same line, or move it "
                    f"into a multiline field such as DESCRIPTION= or CHAPTERS=.",
                    line_no=line_no, field_name=current_key, code="unexpected_continuation",
                )
            existing = parsed.values.get(current_key, "")
            parsed.values[current_key] = f"{existing}\n{raw_line}" if existing else raw_line
    # Trailing newline artefacts introduced by a final blank line.
    for key, value in list(parsed.values.items()):
        if value.endswith("\n"):
            parsed.values[key] = value[:-1]
    return parsed


def parse_metadata_file(
    path: Path | str,
    *,
    max_bytes: int = 1024 * 1024,
) -> ParsedFile:
    """Read and parse a metadata file from disk."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise MetadataParseError(
            f"The metadata file cannot be read: {exc}", code="metadata_unreadable"
        ) from exc
    if size > max_bytes:
        raise MetadataParseError(
            f"The metadata file is {size} bytes; the limit is {max_bytes}. "
            f"That usually means the wrong file was dropped.",
            code="metadata_too_large",
        )
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise MetadataParseError(
            f"The metadata file cannot be opened: {exc}", code="metadata_unreadable"
        ) from exc
    text = _decode(raw, p)
    return parse_metadata_text(text, source_path=str(p), max_length=max_bytes)


# -- typed accessors -------------------------------------------------------

def _parse_bool(value: str, key: str, line_no: int | None) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise MetadataParseError(
        f"{key} must be true or false, not {value.strip()!r}.",
        line_no=line_no, field_name=key, code="invalid_boolean",
    )


def _parse_int(value: str, key: str, line_no: int | None, *, minimum: int = 0) -> int:
    v = value.strip()
    if not v:
        raise MetadataParseError(
            f"{key} must be a number.", line_no=line_no, field_name=key, code="invalid_number"
        )
    try:
        parsed = int(v, 10)
    except ValueError:
        try:
            as_float = float(v)
        except ValueError:
            raise MetadataParseError(
                f"{key} must be a whole number, not {v!r}.",
                line_no=line_no, field_name=key, code="invalid_number",
            ) from None
        if as_float != int(as_float):
            raise MetadataParseError(
                f"{key} must be a whole number, not {v!r}.",
                line_no=line_no, field_name=key, code="invalid_number",
            ) from None
        parsed = int(as_float)
    if parsed < minimum:
        raise MetadataParseError(
            f"{key} must be {minimum} or greater, not {parsed}.",
            line_no=line_no, field_name=key, code="out_of_range",
        )
    return parsed


def _parse_tags(value: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,;]", value) if t.strip()]


def build_metadata(
    parsed: ParsedFile,
    *,
    defaults: dict[str, Any] | None = None,
    max_title_length: int = 400,
    max_description_length: int = 20_000,
) -> EpisodeMetadata:
    """Turn a :class:`ParsedFile` into a canonical :class:`EpisodeMetadata`.

    *Defaults* only fill in fields the user did **not** write.  An explicit
    ``false`` from the user always beats a ``true`` default, because presence
    is tracked per key rather than per truthiness.

    Raises :class:`MetadataParseError` for malformed values.
    """
    defaults = defaults or {}
    ln = parsed.line_numbers
    warnings: list[str] = []
    explicit_fields: set[str] = set()

    def raw(key: str) -> tuple[str, bool]:
        """Return (value, was_explicit).  ``DEFAULT_X`` acts as a fallback."""
        if parsed.has(key):
            explicit_fields.add(key)
            return parsed.get(key), True
        d = defaults.get(key)
        if d not in (None, ""):
            return ("" if d is None else str(d)), False
        return "", False

    title, _ = raw("TITLE")
    description, _ = raw("DESCRIPTION")

    if not title.strip():
        raise MetadataParseError(
            "TITLE is missing. Every episode needs a TITLE= line.",
            line_no=ln.get("TITLE"), field_name="TITLE", code="missing_title",
        )
    if len(title) > max_title_length:
        raise MetadataParseError(
            f"TITLE is {len(title)} characters; the limit is {max_title_length}.",
            line_no=ln.get("TITLE"), field_name="TITLE", code="title_too_long",
        )
    if not description.strip():
        raise MetadataParseError(
            "DESCRIPTION is missing. Spotify requires a description for every "
            "episode - add a DESCRIPTION= line (it may span several lines).",
            line_no=ln.get("DESCRIPTION"), field_name="DESCRIPTION", code="missing_description",
        )
    if len(description) > max_description_length:
        raise MetadataParseError(
            f"DESCRIPTION is {len(description)} characters; the limit is "
            f"{max_description_length}.",
            line_no=ln.get("DESCRIPTION"), field_name="DESCRIPTION", code="description_too_long",
        )

    # Season / episode numbers
    season_raw, _ = raw("SEASON")
    season = _parse_int(season_raw, "SEASON", ln.get("SEASON")) if season_raw.strip() else None
    episode_raw, _ = raw("EPISODE")
    episode_number = _parse_int(episode_raw, "EPISODE", ln.get("EPISODE")) if episode_raw.strip() else None

    # Explicit flag - bool, presence-tracked
    explicit_raw, explicit_given = raw("EXPLICIT")
    if explicit_given:
        explicit = _parse_bool(explicit_raw, "EXPLICIT", ln.get("EXPLICIT"))
    elif isinstance(defaults.get("EXPLICIT"), bool):
        explicit = bool(defaults["EXPLICIT"])
    else:
        explicit = False

    # Timezone
    tz_raw, _ = raw("TIMEZONE")
    tz_name = (tz_raw.strip() or str(defaults.get("TIMEZONE") or "UTC")).strip()
    try:
        resolve_timezone(tz_name)
    except ValueError as exc:
        raise MetadataParseError(
            f"TIMEZONE is not usable: {exc}",
            line_no=ln.get("TIMEZONE"), field_name="TIMEZONE", code="invalid_timezone",
        ) from exc

    # Publish mode
    publish_raw, _ = raw("PUBLISH")
    publish_value = (publish_raw.strip() or "NOW").upper()
    if publish_value in {"SCHEDULED", "SCHEDULE"}:
        publish_mode = PublishMode.SCHEDULE
    elif publish_value in {"NOW", "IMMEDIATE"}:
        publish_mode = PublishMode.NOW
    elif publish_value == "DRAFT":
        publish_mode = PublishMode.DRAFT
    else:
        raise MetadataParseError(
            f"PUBLISH must be NOW, SCHEDULE or DRAFT, not {publish_value!r}.",
            line_no=ln.get("PUBLISH"), field_name="PUBLISH", code="invalid_publish_mode",
        )

    date_raw, _ = raw("PUBLISH_DATE")
    time_raw, _ = raw("PUBLISH_TIME")
    publish_at = None
    if publish_mode is PublishMode.SCHEDULE:
        if not date_raw.strip():
            raise MetadataParseError(
                "PUBLISH=SCHEDULE needs a PUBLISH_DATE=YYYY-MM-DD line.",
                field_name="PUBLISH_DATE", code="missing_publish_date",
            )
        if not time_raw.strip():
            raise MetadataParseError(
                "PUBLISH=SCHEDULE needs a PUBLISH_TIME=HH:MM line.",
                field_name="PUBLISH_TIME", code="missing_publish_time",
            )
        dm = _DATE_RE.match(date_raw.strip())
        if not dm:
            raise MetadataParseError(
                f"PUBLISH_DATE must look like 2026-09-30, not {date_raw.strip()!r}.",
                line_no=ln.get("PUBLISH_DATE"), field_name="PUBLISH_DATE", code="invalid_publish_date",
            )
        tm = _TIME_RE.match(time_raw.strip())
        if not tm:
            raise MetadataParseError(
                f"PUBLISH_TIME must look like 18:00 or 18:00:00, not {time_raw.strip()!r}.",
                line_no=ln.get("PUBLISH_TIME"), field_name="PUBLISH_TIME", code="invalid_publish_time",
            )
        hour, minute = int(tm.group(1)), int(tm.group(2))
        second = int(tm.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise MetadataParseError(
                f"PUBLISH_TIME {time_raw.strip()!r} is not a real time of day.",
                line_no=ln.get("PUBLISH_TIME"), field_name="PUBLISH_TIME", code="invalid_publish_time",
            )
        import datetime as _dt
        naive = _dt.datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)), hour, minute, second)
        try:
            publish_at = from_local_datetime(naive, tz_name)
        except ValueError as exc:
            raise MetadataParseError(
                f"PUBLISH_DATE/PUBLISH_TIME cannot be scheduled: {exc}",
                field_name="PUBLISH_TIME", code="invalid_schedule",
            ) from exc
    else:
        if date_raw.strip() or time_raw.strip():
            warnings.append(
                "PUBLISH_DATE/PUBLISH_TIME are set but PUBLISH is "
                f"{publish_mode.value}; they will be ignored."
            )

    # Chapters
    chapters: list[Chapter] = []
    if parsed.has("CHAPTERS"):
        try:
            chapters = parse_chapters(parsed.get("CHAPTERS"))
        except ChapterLineError as exc:
            raise MetadataParseError(
                f"CHAPTERS could not be read - {exc.reason}",
                line_no=ln.get("CHAPTERS"), field_name="CHAPTERS", code="invalid_chapters",
            ) from exc

    author, _ = raw("AUTHOR")
    show, _ = raw("SHOW")
    language, _ = raw("LANGUAGE")
    notes, _ = raw("NOTES")
    artwork, _ = raw("ARTWORK")
    tags_raw, _ = raw("TAGS")

    return EpisodeMetadata(
        title=title.strip(),
        description=description.strip("\n").strip(),
        season=season,
        episode_number=episode_number,
        explicit=explicit,
        publish=publish_mode,
        publish_at=publish_at,
        timezone=tz_name,
        publish_date_raw=date_raw.strip(),
        publish_time_raw=time_raw.strip(),
        chapters=chapters,
        author=author.strip(),
        show=show.strip(),
        language=language.strip(),
        tags=_parse_tags(tags_raw),
        notes=notes.strip(),
        artwork_path=artwork.strip(),
        raw_text=parsed.raw_text,
        source_path=parsed.source_path,
        warnings=warnings,
        explicit_fields=explicit_fields,
    )
