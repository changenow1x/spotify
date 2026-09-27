"""Metadata parser: format, Unicode, multiline, chapters, rejections."""

from __future__ import annotations

from datetime import timezone

import pytest

from app.metadata.parser import (
    MetadataParseError,
    ParsedFile,
    build_metadata,
    parse_metadata_file,
    parse_metadata_text,
)
from app.core.models import PublishMode

GERMAN = """TITLE=THOT – Der Schlüssel zum Bewusstsein
DESCRIPTION=Eine Reise in die verborgenen Zusammenhänge von Bewusstsein, Herz, Gehirn.
Große Straßen, schöne Aussicht – 100 % authentisch.
SEASON=1
EPISODE=4
EXPLICIT=false
PUBLISH=NOW
"""


def test_simple_pair():
    parsed = parse_metadata_text("TITLE=Hallo\nDESCRIPTION=Welt")
    assert parsed.get("TITLE") == "Hallo"
    assert parsed.get("DESCRIPTION") == "Welt"
    meta = build_metadata(parsed)
    assert meta.title == "Hallo"
    assert meta.description == "Welt"
    assert meta.publish is PublishMode.NOW
    assert meta.explicit is False


def test_german_characters_preserved():
    meta = build_metadata(parse_metadata_text(GERMAN))
    assert meta.title == "THOT – Der Schlüssel zum Bewusstsein"
    assert "Bewusstsein" in meta.description
    assert "Straßen" in meta.description
    assert "Aussicht" in meta.description
    assert "–" in meta.description  # en dash


def test_umlauts_and_emoji_survive():
    text = "TITLE=Über uns 🌀\nDESCRIPTION=Grüße aus München – täglich."
    meta = build_metadata(parse_metadata_text(text))
    assert meta.title == "Über uns 🌀"
    assert "München" in meta.description


def test_multiline_description():
    text = (
        "TITLE=Mehrzeilig\n"
        "DESCRIPTION=Erste Zeile.\n"
        "Zweite Zeile.\n"
        "\n"
        "Nach der Leerzeile.\n"
        "SEASON=2\n"
    )
    meta = build_metadata(parse_metadata_text(text))
    assert meta.description.startswith("Erste Zeile.")
    assert "Zweite Zeile." in meta.description
    assert "Nach der Leerzeile." in meta.description
    assert meta.season == 2
    assert "SEASON=2" not in meta.description


def test_blank_lines_ignored():
    text = "\n\nTITLE=X\n\n\nDESCRIPTION=Y\n\n"
    meta = build_metadata(parse_metadata_text(text))
    assert meta.title == "X"
    assert meta.description == "Y"


def test_crlf_line_endings():
    text = "TITLE=Windows\r\nDESCRIPTION=Auch hier\r\n"
    meta = build_metadata(parse_metadata_text(text))
    assert meta.title == "Windows"
    assert meta.description == "Auch hier"


def test_orphan_line_rejected():
    with pytest.raises(MetadataParseError) as exc:
        parse_metadata_text("just floating text\nTITLE=X")
    assert exc.value.code == "orphan_line"
    assert "KEY=VALUE" in str(exc.value)


def test_missing_title_rejected():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("DESCRIPTION=Nur Beschreibung"))
    assert exc.value.code == "missing_title"
    assert exc.value.field_name == "TITLE"


def test_missing_description_rejected():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("TITLE=Nur Titel"))
    assert exc.value.code == "missing_description"
    assert "DESCRIPTION" in str(exc.value)


def test_title_never_invented_from_a_random_line():
    with pytest.raises(MetadataParseError):
        build_metadata(parse_metadata_text("SEASON=1\nEPISODE=4\nCHAPTERS=\n00:00 Intro"))


def test_unknown_field_collected():
    parsed = parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nTYPO_FILED=1")
    assert "TYPO_FILED" in parsed.unknown
    assert parsed.unknown["TYPO_FILED"][0] == 3


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("yes", True), ("1", True), ("ja", True),
    ("false", False), ("no", False), ("0", False), ("nein", False),
])
def test_boolean_forms(value, expected):
    meta = build_metadata(parse_metadata_text(f"TITLE=X\nDESCRIPTION=Y\nEXPLICIT={value}"))
    assert meta.explicit is expected


def test_invalid_boolean_rejected():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nEXPLICIT=maybe"))
    assert exc.value.code == "invalid_boolean"


def test_user_false_beats_show_default_true():
    """A default of true must never override the user's explicit false."""
    parsed = parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nEXPLICIT=false")
    meta = build_metadata(parsed, defaults={"EXPLICIT": True})
    assert meta.explicit is False
    assert "EXPLICIT" in meta.explicit_fields


def test_default_used_when_absent():
    parsed = parse_metadata_text("TITLE=X\nDESCRIPTION=Y")
    meta = build_metadata(parsed, defaults={"EXPLICIT": True, "TIMEZONE": "Europe/Berlin"})
    assert meta.explicit is True
    assert meta.timezone == "Europe/Berlin"
    assert "EXPLICIT" not in meta.explicit_fields


def test_scheduling_roundtrip():
    text = (
        "TITLE=X\nDESCRIPTION=Y\nPUBLISH=SCHEDULE\n"
        "PUBLISH_DATE=2026-09-30\nPUBLISH_TIME=18:00\nTIMEZONE=Europe/Berlin\n"
    )
    meta = build_metadata(parse_metadata_text(text))
    assert meta.publish is PublishMode.SCHEDULE
    assert meta.publish_at is not None
    assert meta.publish_at.tzinfo is not None
    # 18:00 CEST (UTC+2) in September == 16:00 UTC
    assert meta.publish_at.astimezone(timezone.utc).hour == 16
    assert "2026-09-30" in meta.local_publish_label()


def test_scheduling_requires_date_and_time():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nPUBLISH=SCHEDULE"))
    assert exc.value.code == "missing_publish_date"

    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text(
            "TITLE=X\nDESCRIPTION=Y\nPUBLISH=SCHEDULE\nPUBLISH_DATE=2026-09-30"))
    assert exc.value.code == "missing_publish_time"


@pytest.mark.parametrize("bad", ["30-09-2026", "2026/09/30", "2026-9-30x", "tomorrow"])
def test_invalid_dates_rejected(bad):
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text(
            f"TITLE=X\nDESCRIPTION=Y\nPUBLISH=SCHEDULE\nPUBLISH_DATE={bad}\nPUBLISH_TIME=18:00"))
    assert exc.value.code == "invalid_publish_date"


@pytest.mark.parametrize("bad", ["25:00", "18:75", "noon", "18:0:99"])
def test_invalid_times_rejected(bad):
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text(
            f"TITLE=X\nDESCRIPTION=Y\nPUBLISH=SCHEDULE\nPUBLISH_DATE=2026-09-30\nPUBLISH_TIME={bad}"))
    assert exc.value.code in {"invalid_publish_time"}


def test_invalid_timezone_rejected():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text(
            "TITLE=X\nDESCRIPTION=Y\nTIMEZONE=Mars/Olympus"))
    assert exc.value.code == "invalid_timezone"


def test_dst_gap_rejected_not_silently_shifted():
    """A local time that does not exist must be refused, not moved."""
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text(
            "TITLE=X\nDESCRIPTION=Y\nPUBLISH=SCHEDULE\n"
            "PUBLISH_DATE=2026-03-29\nPUBLISH_TIME=02:30\nTIMEZONE=Europe/Berlin"))
    assert exc.value.code == "invalid_schedule"
    assert "daylight" in str(exc.value).lower() or "does not exist" in str(exc.value)


def test_invalid_publish_mode():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nPUBLISH=LATER"))
    assert exc.value.code == "invalid_publish_mode"


def test_negative_season_rejected():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nSEASON=-1"))
    assert exc.value.code == "out_of_range"


def test_season_accepts_float_looking_int():
    meta = build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nSEASON=2.0"))
    assert meta.season == 2


def test_season_rejects_fraction():
    with pytest.raises(MetadataParseError):
        build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nSEASON=2.5"))


def test_title_too_long():
    long_title = "T" * 50
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text(f"TITLE={long_title}\nDESCRIPTION=Y"),
                       max_title_length=10)
    assert exc.value.code == "title_too_long"


def test_continuation_after_scalar_field_is_rejected():
    """A stray line after TITLE= is a typo, not a longer title."""
    with pytest.raises(MetadataParseError) as exc:
        parse_metadata_text("TITLE=X\njust floating text")
    assert exc.value.code == "unexpected_continuation"
    assert "TITLE" in str(exc.value)


def test_continuation_after_season_is_rejected():
    with pytest.raises(MetadataParseError) as exc:
        parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nSEASON=1\nmore text")
    assert exc.value.code == "unexpected_continuation"


def test_continuation_allowed_in_text_fields():
    parsed = parse_metadata_text(
        "TITLE=X\nDESCRIPTION=Zeile 1\nZeile 2\nNOTES=Notiz 1\nNotiz 2")
    assert parsed.get("DESCRIPTION") == "Zeile 1\nZeile 2"
    assert parsed.get("NOTES") == "Notiz 1\nNotiz 2"


def test_description_too_long():
    with pytest.raises(MetadataParseError) as exc:
        build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=" + "y" * 100),
                       max_description_length=10)
    assert exc.value.code == "description_too_long"


def test_metadata_too_large():
    with pytest.raises(MetadataParseError) as exc:
        parse_metadata_text("TITLE=X\nDESCRIPTION=Y", max_length=5)
    assert exc.value.code == "metadata_too_large"


def test_tags_parsing():
    meta = build_metadata(parse_metadata_text("TITLE=X\nDESCRIPTION=Y\nTAGS=a, b;c"))
    assert meta.tags == ["a", "b", "c"]


def test_file_roundtrip_with_bom(tmp_path):
    path = tmp_path / "ep.txt"
    path.write_bytes("﻿TITLE=With BOM\nDESCRIPTION=Zeile mit Umlaut ä\n".encode("utf-8"))
    parsed = parse_metadata_file(path)
    meta = build_metadata(parsed)
    assert meta.title == "With BOM"
    assert "ä" in meta.description


def test_cp1252_file_is_readable(tmp_path):
    path = tmp_path / "ep.txt"
    path.write_bytes("TITLE=Schluessel\nDESCRIPTION=Straße in cp1252\n".encode("cp1252"))
    meta = build_metadata(parse_metadata_file(path))
    assert "Stra" in meta.description


def test_unreadable_file_reports_clearly(tmp_path):
    with pytest.raises(MetadataParseError) as exc:
        parse_metadata_file(tmp_path / "nope.txt")
    assert exc.value.code == "metadata_unreadable"


def test_oversized_file_rejected(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("TITLE=X\nDESCRIPTION=" + "y" * 5000, encoding="utf-8")
    with pytest.raises(MetadataParseError) as exc:
        parse_metadata_file(path, max_bytes=1000)
    assert exc.value.code == "metadata_too_large"


def test_duplicate_key_appends():
    parsed = parse_metadata_text("TITLE=A\nDESCRIPTION=B\nNOTES=one\nNOTES=two")
    assert parsed.get("NOTES") == "one\ntwo"


def test_raw_text_preserved_exactly():
    text = "TITLE=X\nDESCRIPTION=Y\n"
    parsed = parse_metadata_text(text)
    assert parsed.raw_text == text
    assert build_metadata(parsed).raw_text == text
