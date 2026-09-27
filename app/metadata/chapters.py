"""Chapter marker parsing.

Spotify's *documented* chapter mechanism is a timestamp in the episode
description, e.g. ``(02:15) Das vergessene Wissen`` (see "Adding manual
chapters to your episode description" in the Spotify for Creators help
centre).  The publishing UI has no separate chapter-upload field, so chapters
are modelled as first-class data here and rendered into the description by the
Spotify mapping layer.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..core.models import Chapter

__all__ = [
    "CHAPTER_LINE_RE",
    "ChapterLineError",
    "parse_chapter_line",
    "parse_chapters",
    "format_chapter_timestamp",
    "chapters_to_description_lines",
]

#: ``(MM:SS) Title`` / ``MM:SS Title`` / ``HH:MM:SS - Title`` / ``00:00:00 Title``
#:
#: The timestamp is strict (``M:SS`` / ``MM:SS`` / ``H:MM:SS`` / ``HH:MM:SS``,
#: optionally parenthesised) and a separator plus a non-empty title is
#: required.  That is what stops ``00:00`` from being read as "title ``0``".
CHAPTER_LINE_RE = re.compile(
    r"""^\s*
        (?P<open>\()?                          # optional opening parenthesis
        (?P<a>\d{1,3})\s*:\s*(?P<b>\d{2})
        (?:\s*:\s*(?P<c>\d{2}))?             # optional seconds field
        (?P<close>\))?                         # optional closing parenthesis
        (?P<sep>[\s\-–—:·|.]+)                # required separator
        (?P<title>.+?)
        \s*$""",
    re.VERBOSE,
)

#: A strict timestamp on its own.
_TS_ONLY = re.compile(r"^\(?(\d{1,3})\s*:\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?\)?$")

#: Characters Spotify's chapter renderer will not treat as text.
_HTML_TAG = re.compile(r"<[^>]+>")


class ChapterLineError(ValueError):
    """A single malformed chapter line, with its 1-based line number."""

    def __init__(self, line_no: int, text: str, reason: str):
        self.line_no = line_no
        self.text = text
        self.reason = reason
        super().__init__(f"Chapter line {line_no}: {reason} (got {text!r})")


def format_chapter_timestamp(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _to_seconds(a: str, b: str, c: str | None) -> int:
    first, second = int(a), int(b)
    third = int(c) if c is not None else None
    if second > 59:
        raise ValueError(f"minutes/seconds field {second} is larger than 59")
    if third is not None:
        if third > 59:
            raise ValueError(f"seconds field {third} is larger than 59")
        return first * 3600 + second * 60 + third
    # Two fields: MM:SS (Spotify also accepts M:SS).
    return first * 60 + second


def _unbalanced_paren(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("(") and ")" not in stripped


def parse_chapter_line(line: str, line_no: int = 0) -> Chapter:
    """Parse one chapter line, or raise :class:`ChapterLineError`."""
    if _HTML_TAG.search(line):
        raise ChapterLineError(
            line_no, line, "chapter titles must be plain text; HTML tags are not allowed"
        )
    if _unbalanced_paren(line):
        raise ChapterLineError(
            line_no, line, "the timestamp has an opening '(' but no closing ')'")
    # A bare timestamp gets a precise message instead of a generic one.
    if _TS_ONLY.match(line):
        raise ChapterLineError(line_no, line, "the chapter has a timestamp but no title")
    match = CHAPTER_LINE_RE.match(line)
    if not match:
        raise ChapterLineError(
            line_no, line,
            "expected a timestamp such as '00:00' or '02:15' followed by a title",
        )
    title = match.group("title").strip()
    if not title:
        raise ChapterLineError(line_no, line, "the chapter has a timestamp but no title")
    raw_ts = CHAPTER_LINE_RE.match(line).group(0)[: match.group(0).index(title)].strip()
    raw_ts = raw_ts.rstrip("-–—:·|.").strip()
    try:
        seconds = _to_seconds(match.group("a"), match.group("b"), match.group("c"))
    except ValueError as exc:
        raise ChapterLineError(line_no, line, str(exc)) from exc
    return Chapter(start_seconds=seconds, title=title, raw_timestamp=raw_ts)


def parse_chapters(text: str) -> list[Chapter]:
    """Parse the body of a ``CHAPTERS=`` block.

    Raises :class:`ChapterLineError` on the first malformed line.  Ordering and
    spacing rules are *not* enforced here - those are content rules and belong
    to the validator, which reports them as separate, actionable problems.
    """
    chapters: list[Chapter] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        chapters.append(parse_chapter_line(line, index))
    if not chapters:
        raise ChapterLineError(0, text[:80], "the CHAPTERS block is empty")
    return chapters


def chapters_to_description_lines(chapters: Iterable[Chapter]) -> list[str]:
    """Render chapters exactly the way Spotify documents them."""
    return [c.to_description_line() for c in chapters]
