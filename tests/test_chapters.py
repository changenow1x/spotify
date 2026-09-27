"""Chapter parsing and validation."""

from __future__ import annotations

import pytest

from app.core.models import PublishMode
from app.metadata.chapters import (
    ChapterLineError,
    format_chapter_timestamp,
    parse_chapter_line,
    parse_chapters,
)
from app.metadata.parser import (
    MetadataParseError,
    build_metadata,
    parse_metadata_text,
)
from app.metadata.validator import validate_metadata

VALID = """
TITLE=Kapitel-Test
DESCRIPTION=Beschreibung
CHAPTERS=
00:00 Intro
02:15 Das vergessene Wissen
07:42 Herz und Bewusstsein
"""


def test_parses_valid_chapters():
    meta = build_metadata(parse_metadata_text(VALID))
    assert len(meta.chapters) == 3
    assert [c.start_seconds for c in meta.chapters] == [0, 135, 462]
    assert [c.title for c in meta.chapters] == [
        "Intro", "Das vergessene Wissen", "Herz und Bewusstsein"
    ]


@pytest.mark.parametrize("line,expected", [
    ("00:00 Intro", 0),
    ("0:00 Intro", 0),
    ("00:00:00 Intro", 0),
    ("(00:00) Intro", 0),
    ("01:05 Title", 65),
    ("1:00:00 Title", 3600),
    ("(01:00:00) Title", 3600),
    ("02:15- Das Wissen", 135),
    ("02:15 – Das Wissen", 135),
    ("02:15:Das Wissen", 135),
])
def test_timestamp_formats(line, expected):
    assert parse_chapters(line)[0].start_seconds == expected


@pytest.mark.parametrize("line,reason_fragment", [
    ("Intro", "expected a timestamp"),
    ("00:00", "no title"),
    ("aa:bb Title", "expected a timestamp"),
    ("00:75 Title", "larger than 59"),
    ("00:00:99 Title", "larger than 59"),
    ("<b>00:00</b> Title", "plain text"),
    ("(00:00 Title", "no closing ')'"),
])
def test_malformed_chapter_lines_rejected(line, reason_fragment):
    with pytest.raises(ChapterLineError) as exc:
        parse_chapters(line)
    assert reason_fragment in exc.value.reason


def test_empty_chapters_block_rejected():
    with pytest.raises(ChapterLineError) as exc:
        parse_chapters("\n  \n")
    assert "empty" in exc.value.reason


def test_chapter_titles_not_modified():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 1 - Introduction\n01:00 Zweiter Teil"
    ))
    # Spotify strips leading indexes itself; we must not pre-empt it.
    assert meta.chapters[0].title == "1 - Introduction"


def test_chapters_must_start_at_zero():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n01:00 A\n05:00 B\n09:00 C"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=0)
    assert "chapters_not_starting_at_zero" in report.error_codes()
    assert not report.ok


def test_chapters_must_be_in_order():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 A\n10:00 B\n05:00 C"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=0)
    assert "chapters_out_of_order" in report.error_codes()


def test_duplicate_timestamps_rejected():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 A\n05:00 B\n05:00 C"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=0)
    assert "chapters_duplicate_timestamp" in report.error_codes()


def test_close_chapters_are_a_warning_not_an_error():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 A\n00:10 B\n00:20 C"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert report.ok  # still publishable
    assert "chapters_too_close" in report.warning_codes()


def test_too_few_chapters_is_an_error():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 A\n05:00 B"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=0)
    assert "too_few_chapters" in report.error_codes(), (
        "chapters Spotify will not display must block the episode, not be a warning"
    )
    assert not report.ok


def test_german_chapter_titles():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n"
        "00:00 Einführung\n02:15 Das vergessene Wissen – Grundlagen\n07:42 Zusammenfassung"))
    assert meta.chapters[1].title == "Das vergessene Wissen – Grundlagen"
    assert validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=0).ok


def test_description_line_format_matches_spotify():
    meta = build_metadata(parse_metadata_text(VALID))
    lines = [c.to_description_line() for c in meta.chapters]
    assert lines == ["(00:00) Intro", "(02:15) Das vergessene Wissen",
                     "(07:42) Herz und Bewusstsein"]


def test_hour_format_in_description():
    chapter = parse_chapters("01:02:03 Lang")[0]
    assert chapter.timestamp == "01:02:03"
    assert chapter.to_description_line() == "(01:02:03) Lang"


def test_format_chapter_timestamp_helper():
    assert format_chapter_timestamp(0) == "00:00"
    assert format_chapter_timestamp(59) == "00:59"
    assert format_chapter_timestamp(3600) == "01:00:00"
    assert format_chapter_timestamp(-5) == "00:00"


def test_chapters_without_chapters_block():
    meta = build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y"))
    assert meta.chapters == []
    report = validate_metadata(meta)
    assert report.ok


# ---------------------------------------------------------------------------
# Every manual-chapter rule from Spotify's current chapter documentation.
# https://support.spotify.com/us/creators/article/episode-chapters/
#
# The page states: at least 3 chapters, the first at 00:00, chronological
# order, one chapter per line, at least 30 seconds between start times, and
# plain-text titles with no emoji or HTML.
# ---------------------------------------------------------------------------


def _codes(report):
    """Codes of the entries that BLOCK publication."""
    return set(report.error_codes())


def _all_codes(report):
    """Codes of every entry, errors and warnings alike."""
    return set(report.error_codes()) | set(report.warning_codes())


def test_at_least_three_chapters_is_an_error():
    """Spotify will not show fewer than three, so this must not publish."""
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 One\n02:00 Two"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=0)
    assert "too_few_chapters" in _codes(report)
    assert not report.ok
    assert any("at least 3" in e.message for e in report.errors
               if e.code == "too_few_chapters")


def test_exactly_three_chapters_is_accepted():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 One\n02:00 Two\n05:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "too_few_chapters" not in _all_codes(report)
    assert report.ok


def test_first_chapter_must_start_at_zero():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:30 One\n02:00 Two\n05:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "chapters_not_starting_at_zero" in _codes(report)
    assert not report.ok


def test_chapters_must_be_chronological():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 One\n06:00 Two\n03:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "chapters_out_of_order" in _codes(report)
    assert not report.ok


def test_chapters_closer_than_thirty_seconds_is_reported():
    """Spotify asks for at least 30s between chapter start times.

    This is reported rather than refused, so valid content is never blocked;
    either way the user is told the exact gap.
    """
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00 One\n00:12 Two\n02:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "chapters_too_close" in _all_codes(report)
    entry = next(e for e in report.errors + report.warnings
                 if e.code == "chapters_too_close")
    assert "12s" in entry.message
    assert "30" in entry.message


def test_chapter_html_is_refused_by_the_parser_with_a_line_number():
    """Spotify wants plain text, so HTML is refused - never stripped."""
    with pytest.raises(MetadataParseError) as excinfo:
        build_metadata(parse_metadata_text(
            "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n"
            "00:00 <b>Intro</b>\n02:00 Two\n05:00 Three"))
    message = str(excinfo.value)
    assert "plain text" in message
    assert "line 3" in message, f"the offending line must be named: {message}"


def test_chapter_emoji_is_reported():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n"
        "00:00 Intro 🎬\n02:00 Two\n05:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "chapter_title_emoji" in _all_codes(report)
    assert report.ok


def test_chapter_index_prefix_is_reported_because_spotify_strips_it():
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n"
        "00:00 1 - Introduction\n02:00 Two\n05:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "chapter_title_index" in _all_codes(report)
    assert report.ok


def test_a_title_starting_with_a_number_is_not_mistaken_for_an_index():
    """'11 Ways to cook' is a title, not an index prefix."""
    meta = build_metadata(parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n"
        "00:00 11 Ways to cook\n02:00 Two\n05:00 Three"))
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert "chapter_title_index" not in _all_codes(report)


def test_a_chapter_without_a_title_is_refused_with_a_line_number():
    with pytest.raises(MetadataParseError) as excinfo:
        build_metadata(parse_metadata_text(
            "TITLE=X\nDESCRIPTION=Y\nCHAPTERS=\n00:00\n02:00 Two\n05:00 Three"))
    assert "no title" in str(excinfo.value)
    assert "line 3" in str(excinfo.value)


@pytest.mark.parametrize("line,expected", [
    ("00:00 Intro", "00:00"),
    ("(00:00) Intro", "00:00"),
    # A zero hour component is written in the short form - Spotify accepts
    # both, so this is canonical formatting, not a repair of bad input.
    ("00:00:00 Intro", "00:00"),
    ("(00:00:00) Intro", "00:00"),
    ("01:02:03 Later", "01:02:03"),
    ("(01:02:03) Later", "01:02:03"),
    ("02:15 - Das vergessene Wissen", "02:15"),
])
def test_every_documented_timestamp_form_parses(line, expected):
    """(MM:SS); MM:SS; (HH:MM:SS); HH:MM:SS are all documented forms."""
    assert parse_chapter_line(line).timestamp == expected


def test_an_unparseable_chapter_is_reported_not_repaired():
    with pytest.raises(ValueError):
        parse_chapter_line("no timestamp here at all")
