"""Archiving: originals are moved, never deleted, and never escape the show."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.archive.manager import ArchiveManager


@pytest.fixture
def archiver(config):
    return ArchiveManager(config)


def _drop_incoming(config, show, name: str, content: bytes = b"media bytes") -> Path:
    p = config.show_incoming(show) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class TestArchiveBasics:
    def test_both_files_move_together(self, archiver, config, show):
        media = _drop_incoming(config, show, "ep01.mp4")
        meta = _drop_incoming(config, show, "ep01.txt", b"TITLE=Ep 1\n")

        result = archiver.archive(show, media, meta)

        assert result.ok, result.error
        assert not media.exists() and not meta.exists()
        assert Path(result.media_path).exists()
        assert Path(result.metadata_path).exists()
        assert Path(result.media_path).name == "ep01.mp4"
        assert Path(result.metadata_path).name == "ep01.txt"

    def test_media_only(self, archiver, config, show):
        media = _drop_incoming(config, show, "ep02.mp4")
        result = archiver.archive(show, media, None)
        assert result.ok
        assert result.metadata_path is None
        assert Path(result.media_path).exists()

    def test_nothing_is_ever_deleted(self, archiver, config, show):
        """A move, not a copy-then-delete: the bytes survive."""
        media = _drop_incoming(config, show, "ep03.mp4", b"precious original bytes")
        result = archiver.archive(show, media, None)
        assert Path(result.media_path).read_bytes() == b"precious original bytes"

    def test_extra_files_travel_with_the_episode(self, archiver, config, show):
        media = _drop_incoming(config, show, "ep04.mp4")
        art = _drop_incoming(config, show, "ep04.png", b"png")
        result = archiver.archive(show, media, None, extra_files=[art])
        assert result.ok
        assert (Path(result.destination_dir) / "ep04.png").exists()
        assert not art.exists()

    def test_result_is_serialisable(self, archiver, config, show):
        media = _drop_incoming(config, show, "ep05.mp4")
        data = archiver.archive(show, media, None).as_dict()
        assert data["ok"] is True
        assert data["media_path"].endswith("ep05.mp4")

    def test_destination_is_under_the_show(self, archiver, config, show):
        media = _drop_incoming(config, show, "ep06.mp4")
        result = archiver.archive(show, media, None)
        from app.security.paths import is_within

        assert is_within(config.show_dir(show), Path(result.media_path))

    def test_listing_and_size(self, archiver, config, show):
        for i in range(3):
            archiver.archive(show, _drop_incoming(config, show, f"e{i}.mp4"), None)
        assert archiver.archive_size(show) > 0
        listed = archiver.list_archived(show)
        assert len(listed) == 3
        assert {row["name"] for row in listed} >= {"e0.mp4", "e1.mp4", "e2.mp4"}

    def test_destination_dir_is_grouped_by_year_and_month(self, archiver, config, show):
        when = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        target = archiver.destination_dir(show, when=when)
        assert target.parts[-2:] == ("2026", "05")
        assert target.parent.parent.name == "archive"

    def test_listing_a_show_that_archived_nothing_is_not_an_error(self, archiver, show):
        assert archiver.list_archived(show) == []
        assert archiver.archive_size(show) == 0

    def test_flat_layout_is_supported(self, archiver, config, show):
        config.archive.layout = "flat"
        media = _drop_incoming(config, show, "ep12.mp4")
        result = archiver.archive(show, media, None)
        assert Path(result.destination_dir) == config.show_path(show, "archive")

    def test_a_hostile_layout_falls_back_instead_of_escaping(self, archiver, config, show):
        config.archive.layout = "../../../etc"
        target = archiver.destination_dir(show)
        from app.security.paths import is_within

        assert is_within(config.show_path(show, "archive"), target)


class TestArchiveSafety:
    def test_hostile_filenames_cannot_escape(self, archiver, config, show):
        media = _drop_incoming(config, show, "..evil'; rm -rf ~ $(id).mp4")
        result = archiver.archive(show, media, None)
        assert result.ok, result.error
        from app.security.paths import is_within

        assert is_within(config.show_dir(show), Path(result.media_path))
        assert ".." not in Path(result.media_path).name

    def test_show_id_cannot_redirect_the_destination(self, archiver, config):
        from app.core.config import ShowConfig

        evil = ShowConfig(name="X", id="../../../../etc", enabled=True,
                          spotify_show_id="x")
        media = _drop_incoming(config, config.shows[0], "ep07.mp4")
        result = archiver.archive(evil, media, None)
        if result.ok:
            from app.security.paths import is_within

            assert is_within(config.root, Path(result.media_path))

    def test_two_shows_never_share_an_archive(self, archiver, config, show):
        media = _drop_incoming(config, show, "ep08.mp4")
        result = archiver.archive(show, media, None)
        from app.security.paths import is_within

        for other in config.shows[1:]:
            assert not is_within(config.show_dir(other), Path(result.media_path))

    def test_missing_source_is_reported_not_fatal(self, archiver, config, show):
        result = archiver.archive(show, config.show_incoming(show) / "nope.mp4", None)
        assert result.ok is False
        assert result.error

    def test_destination_stays_inside_the_archive_root(self, archiver, config, show):
        from app.security.paths import is_within

        media = _drop_incoming(config, show, "ep09.mp4")
        result = archiver.archive(show, media, None)
        assert is_within(config.show_path(show, "archive"), Path(result.media_path))


class TestMoveToFailed:
    def test_failed_files_go_to_a_separate_folder(self, archiver, config, show):
        media = _drop_incoming(config, show, "bad.mp4")
        meta = _drop_incoming(config, show, "bad.txt", b"nonsense")
        result = archiver.move_to_failed(show, media, meta)
        assert result.ok, result.error
        assert not media.exists()
        assert "failed" in Path(result.destination_dir).name.lower()
        assert Path(result.media_path).exists()

    def test_failed_files_are_never_deleted(self, archiver, config, show):
        media = _drop_incoming(config, show, "bad2.mp4", b"keep me")
        archiver.move_to_failed(show, media, None)
        found = list((config.show_dir(show) / "failed").rglob("bad2.mp4"))
        assert found and found[0].read_bytes() == b"keep me"


class TestArchiveJobFiles:
    def test_from_a_job_row(self, archiver, config, show, database):
        media = _drop_incoming(config, show, "ep10.mp4")
        meta = _drop_incoming(config, show, "ep10.txt", b"TITLE=T\n")
        job = database.create_job(
            id="job-archive-1", show_id=show.id, base_name="ep10",
            original_filename="ep10.mp4", media_path=str(media),
            metadata_path=str(meta), media_size=media.stat().st_size,
            media_mtime_ns=media.stat().st_mtime_ns, media_kind="video",
        )
        result = archiver.archive_job_files(show, job)
        assert result.ok, result.error
        assert Path(result.media_path).exists()

    def test_job_with_no_metadata(self, archiver, config, show, database):
        media = _drop_incoming(config, show, "ep11.mp4")
        job = database.create_job(
            id="job-archive-2", show_id=show.id, base_name="ep11",
            original_filename="ep11.mp4", media_path=str(media),
            media_size=1, media_mtime_ns=1, media_kind="video",
        )
        assert archiver.archive_job_files(show, job).ok
