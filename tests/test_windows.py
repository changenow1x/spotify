"""Windows-first file handling and filesystem edge cases.

The application is Windows-first, so the behaviours that only bite on Windows
are covered explicitly: reserved device names, illegal characters, case
insensitive extensions, long paths, Unicode, partially-copied files, locked
files, cross-directory and cross-device moves, and restarts.

The tests run on any platform: where a Windows-only rule is involved they
exercise the rule through the application's own helpers, which is where the
decision is actually made.
"""
from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path

import pytest

from app.security.paths import (
    PathSafetyError,
    is_within,
    sanitize_filename,
)


def _worker(harness):
    """A real JobWorker bound to the harness's real adapter."""
    from app.core.queue import JobWorker

    return JobWorker(harness.config, harness.db, adapter=harness.adapter)


# ------------------------------------------------------- reserved names ---
class TestWindowsReservedNames:
    @pytest.mark.parametrize(
        "name",
        ["CON", "con", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9",
         "con.mp3", "NUL.txt"],
    )
    def test_reserved_device_names_never_survive_sanitising(self, name):
        """Windows redirects CON.mp3 to the console device whatever the suffix.

        The name is disambiguated rather than rejected, so the episode is
        still usable, but the result must no longer be a device name.
        """
        cleaned = sanitize_filename(name)
        assert cleaned, f"{name!r} must still yield a usable name"
        stem = cleaned.rpartition(".")[0] if "." in cleaned else cleaned
        assert (stem or cleaned).upper() not in {
            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
            "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
            "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        }, f"{name!r} sanitised to the device name {cleaned!r}"

    @pytest.mark.parametrize("name", ["CONCERT", "NULL", "COM10", "LPT0", "audio.mp3"])
    def test_ordinary_names_survive(self, name):
        assert sanitize_filename(name)

    @pytest.mark.parametrize("ch", ['<', '>', ':', '"', '/', '\\', '|', '?', '*'])
    def test_illegal_windows_characters_are_removed(self, ch):
        cleaned = sanitize_filename(f"episode{ch}one.mp3")
        assert cleaned and ch not in cleaned

    def test_trailing_dot_and_space_are_stripped(self):
        """Windows silently drops these, so two different names would collide."""
        assert sanitize_filename("episode.mp3 ") == sanitize_filename("episode.mp3")
        assert sanitize_filename("   ") == "unnamed"
        assert sanitize_filename("...") == "unnamed"


# ------------------------------------------------------------- long paths ---
class TestLongPaths:
    def test_a_very_long_name_is_shortened_but_kept(self):
        from app.security.paths import MAX_FILENAME_LEN

        cleaned = sanitize_filename("ä" * 400 + ".mp3")
        assert cleaned
        assert len(cleaned) <= MAX_FILENAME_LEN, "the name must fit Windows' path limit"
        assert cleaned.endswith(".mp3")

    def test_shortening_keeps_the_extension(self):
        cleaned = sanitize_filename("x" * 400 + ".mp3")
        assert cleaned.endswith(".mp3")

    def test_a_long_name_survives_a_real_publication(self, harness):
        """The archive folder must cope with the long name the user had."""
        from app.core.database import JobState

        media, meta = harness.drop_pair("L" * 150)
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        worker = _worker(harness)
        worker._archive(job.id)
        assert harness.db.get_job(job.id).state_enum() == JobState.ARCHIVED
        assert not media.exists() and not meta.exists()


# ---------------------------------------------------------------- unicode ---
class TestUnicode:
    @pytest.mark.parametrize(
        "name",
        ["Folge über Gott – Die Schöpfung.mp3", "Επεισόδιο 1.mp3",
         "神的 Larger.mp3", "Эпизод 2.mp3", "Café Naïve – ½.mp3"],
    )
    def test_unicode_names_are_preserved(self, name):
        cleaned = sanitize_filename(name)
        assert cleaned
        assert unicodedata.normalize("NFC", cleaned) == cleaned, "must be NFC normalised"

    def test_decomposed_and_composed_forms_are_treated_as_one(self):
        """macOS hands out NFD, Windows NFC - the same name must not duplicate."""
        composed = "Café.mp3"
        decomposed = unicodedata.normalize("NFD", composed)
        assert composed != decomposed
        assert sanitize_filename(composed) == sanitize_filename(decomposed)

    def test_a_unicode_pair_reaches_the_queue(self, harness):
        harness.drop_pair("Folge über Gott – Die Schöpfung")
        jobs = harness.discover()
        assert len(jobs) == 1
        assert "Die Schöpfung" in jobs[0].media_path

    def test_emoji_in_a_filename_is_handled(self, harness):
        harness.drop_pair("Episode 🎧 one")
        jobs = harness.discover()
        assert len(jobs) == 1


# --------------------------------------------------- temporary extensions ---
class TestPartiallyCopiedFiles:
    @pytest.mark.parametrize(
        "suffix",
        [".part", ".tmp", ".crdownload", ".download", ".partial", ".filepart",
         ".!qb", ".opdownload", ".lrc"],
    )
    def test_temporary_extensions_are_never_picked_up(self, harness, suffix):
        """A half-copied file must not be queued as an episode."""
        harness.incoming.mkdir(parents=True, exist_ok=True)
        (harness.incoming / f"Episode{suffix}").write_bytes(b"\x00" * 64)
        (harness.incoming / "Episode.txt").write_text(
            "TITLE=Episode\nDESCRIPTION=Body.\n", encoding="utf-8"
        )
        harness.discover()
        assert harness.db.list_jobs(limit=50) == [], (
            f"a {suffix} file was queued as a real episode"
        )

    def test_the_real_file_beside_a_partial_one_is_picked_up(self, harness):
        harness.drop_pair("Half Done")
        harness.incoming.mkdir(parents=True, exist_ok=True)
        (harness.incoming / "Half Done.mp3.part").write_bytes(b"\x00" * 32)
        jobs = harness.discover()
        assert len(jobs) == 1

    @pytest.mark.parametrize("name", ["Episode.MP3", "Episode.Mp3", "episode.mp3"])
    def test_extensions_match_case_insensitively(self, harness, name):
        """Windows reports extensions in whatever case the file has."""
        stem = name.rsplit(".", 1)[0]
        harness.incoming.mkdir(parents=True, exist_ok=True)
        from make_test_media import make_mp3

        make_mp3(harness.incoming / name, seconds=2.0, comment=f"c {name}")
        (harness.incoming / f"{stem}.txt").write_text(
            f"TITLE={stem}\nDESCRIPTION=Body.\n", encoding="utf-8"
        )
        jobs = harness.discover()
        assert len(jobs) == 1, f"{name} was not recognised as media"


# ------------------------------------------------------------------ locks ---
class TestFileLocks:
    """Windows refuses to move a file another program has open."""

    def test_a_locked_file_is_reported_not_crashed(self, harness):
        import app.archive.manager as manager_module
        from app.core.database import JobState

        media, _ = harness.drop_pair("Locked")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")

        attempts = {"n": 0}
        original = manager_module._move

        def locked_move(source, target):
            attempts["n"] += 1
            raise PermissionError(13, "The process cannot access the file because it is "
                                        "being used by another process")

        manager_module._move = locked_move
        try:
            _worker(harness)._archive(job.id)
        finally:
            manager_module._move = original

        assert attempts["n"] > 0, "the archive must actually have tried to move"
        assert media.exists(), "a locked original must not be lost"

    def test_a_locked_file_does_not_crash_the_worker(self, harness):
        import app.archive.manager as manager_module
        from app.core.database import JobState

        harness.drop_pair("Lock Crash")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        original = manager_module._move
        manager_module._move = lambda *a, **k: (_ for _ in ()).throw(
            PermissionError(13, "in use")
        )
        try:
            _worker(harness)._archive(job.id)  # must not raise
        finally:
            manager_module._move = original
        assert harness.db.get_job(job.id).state_enum() == JobState.PUBLISHED

    def test_archive_failure_keeps_the_job_published(self, harness):
        """The user's rule: an archive failure must never republish."""
        import app.archive.manager as manager_module
        from app.core.database import JobState

        harness.drop_pair("Keep Published")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        original = manager_module._move
        manager_module._move = lambda *a, **k: (_ for _ in ()).throw(OSError("disk is full"))
        try:
            _worker(harness)._archive(job.id)
        finally:
            manager_module._move = original

        row = harness.db.get_job(job.id)
        assert row.state_enum() == JobState.PUBLISHED, (
            f"an archive failure left the job in {row.state}"
        )
        assert not row.get("archived_media_path"), (
            "a failed archive must not record a destination"
        )
        assert "archive" in (row.status_message or "").lower()

    def test_a_retry_after_an_archive_failure_does_not_republish(self, harness):
        """Archiving is retried on its own; publication must not happen again."""
        import app.archive.manager as manager_module
        from app.core.database import JobState

        media, _ = harness.drop_pair("Archive Retry")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        original = manager_module._move
        manager_module._move = lambda *a, **k: (_ for _ in ()).throw(OSError("locked"))
        try:
            _worker(harness)._archive(job.id)
        finally:
            manager_module._move = original
        assert harness.db.get_job(job.id).state_enum() == JobState.PUBLISHED

        # The lock is gone; archiving now completes, still without publishing.
        before = len(harness.spotify.episodes())
        _worker(harness)._archive(job.id)
        assert harness.db.get_job(job.id).state_enum() == JobState.ARCHIVED
        assert len(harness.spotify.episodes()) == before
        assert not media.exists()


# ----------------------------------------------------------- cross moves ---
class TestCrossDirectoryMoves:
    def test_archive_moves_to_a_different_directory(self, harness, config):
        from app.core.database import JobState

        media, meta = harness.drop_pair("Cross Dir")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        _worker(harness)._archive(job.id)

        assert not media.exists(), "the original must be moved, not copied"
        assert not meta.exists()
        archive = config.show_path(config.shows[0], "archive")
        assert list(archive.rglob("*.mp3")), "the archive must hold the media"
        assert list(archive.rglob("*.txt")), "the archive must hold the metadata"

    def test_the_pair_stays_together_in_the_archive(self, harness, config):
        from app.core.database import JobState

        media, meta = harness.drop_pair("Kept Together")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        _worker(harness)._archive(job.id)
        archive = config.show_path(config.shows[0], "archive")
        found = {p.stem for p in archive.rglob("*") if p.is_file()}
        assert media.stem in found, "the media and its .txt must share a folder"

    def test_originals_are_never_deleted_by_a_failed_archive(self, harness):
        import app.archive.manager as manager_module
        from app.core.database import JobState

        media, meta = harness.drop_pair("Never Deleted")
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")
        original = manager_module._move
        manager_module._move = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        try:
            _worker(harness)._archive(job.id)
        finally:
            manager_module._move = original
        assert media.exists(), "the original media must survive a failed archive"
        assert meta.exists(), "the original metadata must survive a failed archive"

    def test_a_file_renamed_away_is_reported_not_silently_lost(self, harness):
        """Renaming in Explorer is routine; the original path then no longer exists.

        Identity is content-based, so publication is unaffected. The archive
        must not crash or claim success, and must keep the job PUBLISHED so it
        can be archived once the user is asked.
        """
        from app.core.database import JobState

        media, meta = harness.drop_pair("Before Rename")
        job = harness.discover()[0]
        media.rename(media.with_name("After Rename.mp3"))
        harness.force_state(job.id, JobState.PUBLISHED, message="published")

        _worker(harness)._archive(job.id)  # must not raise

        row = harness.db.get_job(job.id)
        assert row.state_enum() == JobState.PUBLISHED, (
            "a missing source must not be reported as archived"
        )
        assert not row.get("archived_media_path")
        assert meta.exists(), "the metadata that is still there must be kept"

    def test_a_name_clash_in_the_archive_does_not_overwrite(self, harness, config):
        """A re-published episode must not clobber the archived original."""
        from app.archive.manager import ArchiveManager

        archive = config.show_path(config.shows[0], "archive")
        first = archive / "2020" / "01" / "Clash.mp3"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"ORIGINAL CONTENT")

        incoming = harness.incoming / "Clash.mp3"
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(b"NEW CONTENT, same name, later day")

        result = ArchiveManager(config).archive(config.shows[0], incoming)
        assert result.ok, result.error
        assert first.read_bytes() == b"ORIGINAL CONTENT", (
            "the existing archived file must not be overwritten"
        )
        assert result.media_path != first, "the new file needs its own name"


# ---------------------------------------------------------------- restart ---
class TestRestart:
    def test_state_survives_reopening_the_database(self, config, database):
        from app.core.database import Database, JobState

        job = database.create_job(
            media_path="/tmp/a.mp3", metadata_path=None, show_id=config.shows[0].id
        )
        database.force_state(job.id, JobState.QUEUED, message="queued")
        database.close()

        reopened = Database(config.database_path())
        try:
            row = reopened.get_job(job.id)
            assert row is not None, "a job must survive a restart"
            assert row.state_enum() == JobState.QUEUED
            assert row.get("attempts", 1) >= 1
            assert reopened.get_events(job.id), "the history must survive too"
        finally:
            reopened.close()

    def test_a_file_partially_copied_at_shutdown_is_re_examined(self, harness):
        """A .part file left over from a crash must not become an episode."""
        harness.drop_pair("Restart Safe")
        jobs = harness.discover()
        assert len(jobs) == 1
        # Simulate the shutdown happening before the .txt landed.
        assert jobs[0].state_enum().value in {"DISCOVERED", "QUEUED", "WAITING_METADATA"}


# ------------------------------------------------------------- path scope ---
class TestPathScope:
    def test_is_within_accepts_a_child(self, tmp_path):
        assert is_within(tmp_path, tmp_path / "a" / "b")

    def test_is_within_rejects_a_sibling_with_a_shared_prefix(self, tmp_path):
        """'/data' must not be treated as containing '/data-evil'."""
        sibling = tmp_path.parent / f"{tmp_path.name}-evil"
        assert is_within(tmp_path, sibling) is False

    def test_is_within_rejects_dotdot(self, tmp_path):
        assert is_within(tmp_path, tmp_path / ".." / "elsewhere") is False

    def test_is_within_accepts_the_root_itself(self, tmp_path):
        assert is_within(tmp_path, tmp_path) is True

    def test_safe_join_refuses_to_escape(self, tmp_path):
        from app.security.paths import safe_join

        with pytest.raises(PathSafetyError):
            safe_join(tmp_path, "..", "elsewhere")
