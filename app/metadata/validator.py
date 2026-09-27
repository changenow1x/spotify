"""Metadata validation.

Two distinct jobs, kept separate on purpose:

``validate_parsed``  structure of the ``.txt`` file (unknown keys, empty
                     values, multiline sanity)
``validate_metadata`` semantic checks on the built model (chapter ordering,
                     schedule in the past, title/show mismatch)

Both return a :class:`ValidationReport`, so a bad file always produces a full
list of problems instead of only the first one.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..core.models import EpisodeMetadata, PublishMode, ValidationReport
from .chapters import _HTML_TAG
from .parser import DERIVED_FIELDS, KNOWN_FIELDS, MetadataParseError, ParsedFile

__all__ = ["validate_parsed", "validate_metadata", "validate_episode"]


def validate_parsed(parsed: ParsedFile, *, allow_unknown: bool = False) -> ValidationReport:
    """Check the raw file for structural problems before building a model."""
    report = ValidationReport()
    for key, (line_no, value) in sorted(parsed.unknown.items(), key=lambda kv: kv[1][0]):
        suggestion = _closest_key(key)
        if suggestion:
            report.add_error(
                "unknown_field",
                f"Unknown field {key!r} on line {line_no}. Did you mean {suggestion!r}?",
                field_=key,
                hint=f"Valid fields: {', '.join(sorted(KNOWN_FIELDS))}",
            )
        else:
            report.add_error(
                "unknown_field",
                f"Unknown field {key!r} on line {line_no}.",
                field_=key,
                hint=f"Valid fields: {', '.join(sorted(KNOWN_FIELDS))}",
            )
    if not allow_unknown:
        for key in parsed.values:
            if key in parsed.unknown:
                continue
            if key not in KNOWN_FIELDS and key not in DERIVED_FIELDS:
                report.add_error("unknown_field", f"Unknown field {key!r}.", field_=key)

    for key, value in parsed.values.items():
        if key in {"TITLE", "DESCRIPTION", "CHAPTERS", "NOTES", "ARTWORK",
                   "AUTHOR", "SHOW", "LANGUAGE"}:
            continue
        if not value.strip():
            report.add_error(
                "empty_value",
                f"{key} is present but has no value.",
                field_=key,
                hint="Either give it a value or delete the line.",
            )
    return report


#: Chapter titles are plain text per Spotify's chapter guidance.  These are
#: used to *warn*, never to refuse - they cannot change what the episode is.
_MARKUP_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|&(?:[a-zA-Z]+|#\d+);")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]"
)
#: "1 - Intro", "2. Intro", "03) Intro" - Spotify strips the index.
_INDEX_PREFIX_RE = re.compile(r"^\s*\d{1,3}\s*(?:[-.):]|\u2013\s?)\s*\S")


def _closest_key(key: str) -> str | None:
    """Cheap edit-distance suggestion so typos are obvious, not mysterious."""
    import difflib

    candidates = sorted(KNOWN_FIELDS) + sorted(DERIVED_FIELDS)
    matches = difflib.get_close_matches(key.upper(), candidates, n=1, cutoff=0.75)
    return matches[0] if matches else None


def validate_metadata(
    meta: EpisodeMetadata,
    *,
    min_chapters: int = 3,
    min_chapter_gap_seconds: int = 30,
    max_chapters: int = 200,
    show_name: str = "",
    require_description: bool = True,
    require_title: bool = True,
    now: datetime | None = None,
) -> ValidationReport:
    """Semantic validation of the canonical metadata model."""
    report = ValidationReport()

    if require_title and not meta.title.strip():
        report.add_error("missing_title", "TITLE is missing.", field_="TITLE")
    if _HTML_TAG.search(meta.title):
        report.add_warning(
            "title_contains_html",
            "TITLE contains HTML tags; they will be shown literally.",
            field_="TITLE",
        )
    if len(meta.title.strip()) > 255:
        report.add_warning(
            "title_very_long",
            f"TITLE is {len(meta.title)} characters. Podcast platforms commonly "
            f"truncate around 100-255 characters, so it may be cut off.",
            field_="TITLE",
        )

    if require_description and not meta.description.strip():
        report.add_error(
            "missing_description",
            "DESCRIPTION is missing. Spotify requires a description for every episode.",
            field_="DESCRIPTION",
        )

    if meta.season is not None and meta.season < 0:
        report.add_error("invalid_season", "SEASON cannot be negative.", field_="SEASON")
    if meta.episode_number is not None and meta.episode_number < 0:
        report.add_error("invalid_episode", "EPISODE cannot be negative.", field_="EPISODE")
    if meta.episode_number is not None and meta.season is None:
        report.add_warning(
            "episode_without_season",
            "EPISODE is set but SEASON is not. Spotify will number the episode "
            "outside a season.",
            field_="SEASON",
        )

    if show_name and meta.show and meta.show.strip().lower() != show_name.strip().lower():
        report.add_warning(
            "show_name_mismatch",
            f"SHOW is {meta.show!r} but this episode is being published to "
            f"{show_name!r}. The configured show folder wins.",
            field_="SHOW",
        )

    # Schedule sanity
    if meta.publish is PublishMode.SCHEDULE:
        if meta.publish_at is None:
            report.add_error(
                "missing_schedule",
                "PUBLISH=SCHEDULE but no usable date/time was given.",
                field_="PUBLISH",
            )
        else:
            reference = now or datetime.now(timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            if meta.publish_at <= reference:
                report.add_error(
                    "schedule_in_past",
                    f"The scheduled time {meta.local_publish_label()} is in the past. "
                    f"Spotify cannot schedule an episode retroactively - use PUBLISH=NOW "
                    f"or pick a future time.",
                    field_="PUBLISH_DATE",
                )
            else:
                delta_hours = (meta.publish_at - reference).total_seconds() / 3600.0
                if delta_hours < 1:
                    report.add_warning(
                        "schedule_soon",
                        f"The episode goes live in under an hour "
                        f"({meta.local_publish_label()}).",
                        field_="PUBLISH_DATE",
                    )

    # Chapters
    chapters = meta.chapters
    if chapters:
        if len(chapters) > max_chapters:
            report.add_error(
                "too_many_chapters",
                f"There are {len(chapters)} chapters; the limit is {max_chapters}.",
                field_="CHAPTERS",
            )
        if chapters[0].start_seconds != 0:
            report.add_error(
                "chapters_not_starting_at_zero",
                f"The first chapter must start at 00:00, but it starts at "
                f"{chapters[0].timestamp}.",
                field_="CHAPTERS",
                hint="Spotify ignores chapters that do not begin at zero.",
            )
        for index in range(1, len(chapters)):
            prev, current = chapters[index - 1], chapters[index]
            if current.start_seconds == prev.start_seconds:
                report.add_error(
                    "chapters_duplicate_timestamp",
                    f"Chapters {index} and {index + 1} both start at "
                    f"{current.timestamp}.",
                    field_="CHAPTERS",
                )
            elif current.start_seconds < prev.start_seconds:
                report.add_error(
                    "chapters_out_of_order",
                    f"Chapter {index + 1} starts at {current.timestamp}, which is "
                    f"before chapter {index} ({prev.timestamp}). Chapters must be "
                    f"in chronological order.",
                    field_="CHAPTERS",
                )
            elif current.start_seconds - prev.start_seconds < min_chapter_gap_seconds:
                report.add_warning(
                    "chapters_too_close",
                    f"Chapter {index + 1} starts only "
                    f"{current.start_seconds - prev.start_seconds}s after chapter {index}. "
                    f"Spotify recommends at least {min_chapter_gap_seconds}s.",
                    field_="CHAPTERS",
                )
        for chapter in chapters:
            if not chapter.title.strip():
                report.add_error(
                    "chapter_without_title",
                    f"The chapter at {chapter.timestamp} has no title.",
                    field_="CHAPTERS",
                )
            elif len(chapter.title) > 120:
                report.add_warning(
                    "chapter_title_long",
                    f"The chapter title at {chapter.timestamp} is "
                    f"{len(chapter.title)} characters. Spotify suggests keeping "
                    f"chapter titles short (under ~40 characters).",
                    field_="CHAPTERS",
                )
            else:
                # Spotify's chapter guidance: titles are plain text, with no
                # markup and no emoji, and an index prefix is stripped.
                if _MARKUP_RE.search(chapter.title):
                    report.add_warning(
                        "chapter_title_markup",
                        f"The chapter title at {chapter.timestamp} contains HTML "
                        f"or markup. Spotify expects plain text and will not "
                        f"render it as a chapter title.",
                        field_="CHAPTERS",
                    )
                if _EMOJI_RE.search(chapter.title):
                    report.add_warning(
                        "chapter_title_emoji",
                        f"The chapter title at {chapter.timestamp} contains an "
                        f"emoji. Spotify asks for plain-text chapter titles.",
                        field_="CHAPTERS",
                    )
                if _INDEX_PREFIX_RE.match(chapter.title):
                    report.add_warning(
                        "chapter_title_index",
                        f"The chapter title at {chapter.timestamp} starts with an "
                        f"index. Spotify removes it when it displays the chapter, "
                        f"so the listener sees a different title than the one here.",
                        field_="CHAPTERS",
                    )
        if min_chapters and len(chapters) < min_chapters:
            # An error, not a warning.  Spotify will accept the episode but
            # will not show the chapters to listeners, and publishing anyway
            # would mean quietly delivering something other than what the
            # user asked for.
            report.add_error(
                "too_few_chapters",
                f"There are only {len(chapters)} chapters. Spotify needs at least "
                f"{min_chapters} for chapters to be shown to listeners.",
                field_="CHAPTERS",
                hint=("Either add more chapters, or remove the CHAPTERS block to "
                      "publish the episode without them."),
            )
    for warning in meta.warnings:
        report.add_warning("metadata_note", warning)

    report.info["chapter_count"] = len(chapters)
    report.info["scheduled"] = meta.is_scheduled
    report.info["publish_label"] = meta.local_publish_label() if meta.is_scheduled else "now"
    return report


def validate_episode(
    parsed: ParsedFile,
    meta: EpisodeMetadata | None,
    *,
    parse_error: MetadataParseError | None = None,
    show_name: str = "",
    **kwargs: Any,
) -> ValidationReport:
    """Combined structural + semantic report (the one the worker uses)."""
    report = validate_parsed(parsed) if parsed is not None else ValidationReport()
    if parse_error is not None:
        report.add_error(
            parse_error.code,
            str(parse_error),
            field_=parse_error.field_name,
            hint=_hint_for(parse_error.code),
        )
    if meta is not None:
        semantic = validate_metadata(meta, show_name=show_name, **kwargs)
        report.errors.extend(semantic.errors)
        report.warnings.extend(semantic.warnings)
        report.info.update(semantic.info)
    report.ok = not report.errors
    return report


def _hint_for(code: str) -> str:
    return {
        "missing_title": "Add a line starting with TITLE=.",
        "missing_description": "Add a line starting with DESCRIPTION=.",
        "unknown_field": "Remove the line or fix the spelling.",
        "orphan_line": "Only lines belonging to an empty KEY= block may be free text.",
        "invalid_chapters": "Each chapter line must be 'MM:SS Title'.",
        "metadata_too_large": "Check that you dropped the right .txt file.",
        "invalid_timezone": "Use an IANA name such as Europe/Berlin.",
    }.get(code, "")
