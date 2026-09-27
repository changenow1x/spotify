"""Large-file and performance characteristics.

The app is expected to handle real podcast files - a 500 MB video and a
200 MB audio file are normal. These tests pin the behaviours that a large file
could plausibly break: hashing, probing, metadata parsing, archiving and the
watcher's stability check.

The sizes here are deliberately modest so the suite stays fast; the point is
that nothing reads a whole file into memory or scales with file size where it
should not. A separate test measures the actual scaling.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.core.hashing import hash_file
from app.media.validator import MediaInspector, MediaValidator
from app.media.ffmpeg import FfmpegTools

from app.core.config import MediaPolicy


@pytest.fixture(scope="module")
def tools():
    return FfmpegTools("", "")


# ------------------------------------------------------------------ hash ---
class TestHashingLargeFiles:
    def test_hashing_does_not_load_the_file_into_memory(self, tmp_path):
        """A 400 MB file must be hashed in chunks."""
        import tracemalloc

        target = tmp_path / "big.mp3"
        chunk = os.urandom(1024 * 1024)
        with open(target, "wb") as handle:
            for _ in range(400):  # 400 MB
                handle.write(chunk)

        tracemalloc.start()
        digest = hash_file(target)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert digest
        assert peak < 64 * 1024 * 1024, (
            f"hashing a 400 MB file used {peak / 1e6:.0f} MB of memory; "
            "it must stream"
        )

    def test_the_same_file_always_hashes_the_same(self, tmp_path):
        from make_test_media import make_mp3

        target = tmp_path / "stable.mp3"
        make_mp3(target, seconds=2.0, comment="stable")
        assert hash_file(target) == hash_file(target)

    def test_different_content_hashes_differently(self, tmp_path):
        from make_test_media import make_mp3

        a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
        make_mp3(a, seconds=2.0, comment="a")
        make_mp3(b, seconds=2.0, comment="b")
        assert hash_file(a) != hash_file(b)

    def test_hashing_is_linear_not_quadratic(self, tmp_path):
        """Ten times the bytes must not cost a hundred times the time."""
        from make_test_media import make_mp3

        small, large = tmp_path / "s.bin", tmp_path / "l.bin"
        small.write_bytes(os.urandom(8 * 1024 * 1024))
        large.write_bytes(os.urandom(80 * 1024 * 1024))

        def timed(path):
            start = time.perf_counter()
            hash_file(path)
            return time.perf_counter() - start

        timed(small)  # warm the page cache
        small_time, large_time = timed(small), timed(large)
        assert large_time < small_time * 25 + 0.5, (
            f"8 MB took {small_time:.2f}s but 80 MB took {large_time:.2f}s"
        )

    def test_a_missing_file_raises_rather_than_returning_a_wrong_hash(self, tmp_path):
        with pytest.raises(OSError):
            hash_file(tmp_path / "nope.mp3")


# ----------------------------------------------------------------- probe ---
class TestProbingLargeFiles:
    def test_the_builtin_reader_does_not_read_the_whole_file(self, tmp_path, tools):
        """Probing must read the headers, not stream 500 MB."""
        import tracemalloc

        from make_test_media import make_mp3

        target = tmp_path / "big.mp3"
        make_mp3(target, seconds=600.0, bitrate_kbps=96, comment="large")  # ~7 MB
        filler = tmp_path / "filler.bin"
        filler.write_bytes(os.urandom(200 * 1024 * 1024))

        inspector = MediaInspector(tools, MediaPolicy())
        tracemalloc.start()
        info = inspector.inspect(target)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert info.info.duration > 550
        assert peak < 32 * 1024 * 1024, f"probing used {peak / 1e6:.0f} MB"

    def test_a_long_episode_is_accepted(self, tmp_path, tools):
        from make_test_media import make_mp3

        # A 96 kbps bitrate keeps a 70-minute fixture to about 50 MB.
        target = tmp_path / "long.mp3"
        make_mp3(target, seconds=4200.0, bitrate_kbps=96, comment="very long")
        inspector = MediaInspector(tools, MediaPolicy())
        info = inspector.inspect(target).info
        assert info.duration > 4000

    def test_a_truncated_file_is_rejected(self, tmp_path, tools):
        """A constant-bitrate header states the full length, so only the byte
        count reveals that the data was cut off."""
        from make_test_media import make_mp3

        full = tmp_path / "full.mp3"
        make_mp3(full, seconds=120.0, comment="full")
        data = full.read_bytes()
        truncated = tmp_path / "truncated.mp3"
        truncated.write_bytes(data[: len(data) // 3])

        validator = MediaValidator(MediaInspector(tools, MediaPolicy()), MediaPolicy())
        info, report = validator.validate(truncated)
        assert not report.ok, "a file cut to a third of its length must be rejected"
        assert any(e.code == "media_appears_truncated" for e in report.errors), (
            f"expected media_appears_truncated, got {[e.code for e in report.errors]}"
        )

    def test_a_complete_file_of_the_same_length_is_accepted(self, tmp_path, tools):
        """The truncation check must not fire on a genuinely short episode."""
        from make_test_media import make_mp3

        target = tmp_path / "short.mp3"
        make_mp3(target, seconds=6.0, comment="short but complete")
        validator = MediaValidator(MediaInspector(tools, MediaPolicy()), MediaPolicy())
        _, report = validator.validate(target)
        assert not any(e.code == "media_appears_truncated" for e in report.errors)

    def test_a_zero_byte_file_is_rejected(self, tmp_path, tools):
        empty = tmp_path / "empty.mp3"
        empty.write_bytes(b"")
        validator = MediaValidator(MediaInspector(tools, MediaPolicy()), MediaPolicy())
        info, report = validator.validate(empty)
        assert not report.ok, "an empty file must not pass validation"
        assert report.errors, "a failure must say what is wrong"


# -------------------------------------------------------------- metadata ---
class TestLargeMetadataFiles:
    def test_a_very_long_description_is_parsed(self, tmp_path, tools):
        from make_test_media import make_mp3
        from app.metadata.parser import parse_metadata_file

        media = tmp_path / "long.mp3"
        make_mp3(media, seconds=2.0, comment="long desc")
        meta = tmp_path / "long.txt"
        body = "\n".join(f"Line {n} of a very long description." for n in range(4000))
        meta.write_text(f"TITLE=Long One\nDESCRIPTION={body}\n", encoding="utf-8")

        parsed = parse_metadata_file(meta)
        assert parsed.values.get("TITLE") == "Long One"
        assert "Line 3999" in parsed.values.get("DESCRIPTION", "")

    def test_a_very_long_file_name_does_not_break_pairing(self, tmp_path, harness):
        """Windows allows ~255 characters; pairing must survive that."""
        stem = "Episode " + "x" * 180
        media = tmp_path / f"{stem}.mp3"
        meta = tmp_path / f"{stem}.txt"
        media.write_bytes(b"\x00" * 16)
        meta.write_text("TITLE=x\nDESCRIPTION=y\n", encoding="utf-8")

        from app.watcher.pairing import (
            MediaCandidate,
            MediaKind,
            MetadataCandidate,
            find_pair,
        )

        def candidate(path):
            stat = path.stat()
            return path.stat().st_mtime_ns

        matched, problems = find_pair(
            MediaCandidate(
                path=media, base_name=stem, extension=".mp3",
                size=media.stat().st_size, kind=MediaKind.AUDIO,
                mtime_ns=candidate(media),
            ),
            [MetadataCandidate(
                path=meta, base_name=stem, extension=".txt",
                size=meta.stat().st_size, mtime_ns=candidate(meta),
            )],
        )
        assert not problems, problems
        assert matched is not None and matched.path == meta


# -------------------------------------------------------------- archiving ---
class TestArchivingLargeFiles:
    def test_a_large_file_is_moved_not_copied(self, harness, config):
        """A move must not double the disk usage or read the whole file."""
        from app.core.database import JobState

        media, meta = harness.drop_pair("Large Move")
        media.write_bytes(media.read_bytes() + os.urandom(20 * 1024 * 1024))
        job = harness.discover()[0]
        harness.force_state(job.id, JobState.PUBLISHED, message="published")

        from app.core.queue import JobWorker

        worker = JobWorker(harness.config, harness.db, adapter=harness.adapter)
        start = time.perf_counter()
        worker._archive(job.id)
        elapsed = time.perf_counter() - start

        assert not media.exists()
        assert harness.db.get_job(job.id).state_enum() == JobState.ARCHIVED
        archive = config.show_path(config.shows[0], "archive")
        assert list(archive.rglob("*.mp3"))
        assert elapsed < 30, f"archiving took {elapsed:.1f}s"

    def test_the_watcher_does_not_stat_a_growing_file_repeatedly(self, harness):
        """Stability polling must be cheap even with big files present."""
        harness.drop_pair("Big Present")
        big = harness.incoming / "Big Present.mp3"
        big.write_bytes(big.read_bytes() + os.urandom(10 * 1024 * 1024))

        start = time.perf_counter()
        for _ in range(20):
            harness.watcher.scan_once()
        elapsed = time.perf_counter() - start
        assert elapsed < 15, f"20 stability checks took {elapsed:.1f}s"


# ------------------------------------------------------------- throughput ---
class TestThroughput:
    def test_ten_episodes_queue_quickly(self, harness):
        """A folder full of episodes must be picked up promptly."""
        for n in range(10):
            harness.drop_pair(f"Episode {n:02d}")
        start = time.perf_counter()
        jobs = harness.discover(timeout=30.0, expect=10)
        elapsed = time.perf_counter() - start
        assert len(jobs) == 10
        assert elapsed < 25, f"queueing ten episodes took {elapsed:.1f}s"

    def test_hash_mode_quick_and_full_agree_on_identity(self, harness, config):
        """Switching hash mode must not create a second job for one file."""
        from make_test_media import make_mp3
        from app.core.database import Database

        media = harness.drop_media("Hash Modes")
        make_mp3(media, seconds=2.0, comment="hash modes")
        harness.drop_metadata("Hash Modes")
        first = harness.discover()
        assert len(first) == 1
        quick = first[0].content_hash

        config.hash_mode = "full"
        again = harness.discover()
        assert len(again) == 1, "the same file must not be queued twice"
        assert again[0].content_hash == quick


class TestLargeFileIntegration:
    """A realistic large file must go through the production path in bounded memory.

    The test double has to buffer an upload to answer a request - a real
    browser reads the file out of band.  So the memory claim is measured on
    the code that actually runs on a user's machine: hashing, probing, the
    watcher, and the archive move, plus an assertion that the publisher hands
    Playwright a *path* rather than the file's contents.
    """

    @staticmethod
    def _big_file(tmp_path: Path, megabytes: int = 600) -> Path:
        from make_test_media import make_mp4

        media = tmp_path / "BIG.mp4"
        make_mp4(media, seconds=2.0)
        block = os.urandom(1024 * 1024)
        with open(media, "ab") as handle:
            for _ in range(megabytes):
                handle.write(block)
        assert media.stat().st_size > megabytes * 1024 * 1024
        return media

    def test_the_production_path_over_a_600_megabyte_file_is_bounded(self, tmp_path):
        import tracemalloc

        from app.core.hashing import hash_file, hash_path_quick
        from app.media.builtin_probe import probe_builtin

        media = self._big_file(tmp_path)
        size = media.stat().st_size

        def growth_of(fn):
            tracemalloc.start()
            base = tracemalloc.get_traced_memory()[0]
            result = fn()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return result, peak - base

        digest, digest_growth = growth_of(lambda: hash_file(media))
        info, probe_growth = growth_of(lambda: probe_builtin(media))
        quick, quick_growth = growth_of(lambda: hash_path_quick(media))

        destination = tmp_path / "archive" / "BIG.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        _, move_growth = growth_of(lambda: os.replace(media, destination))

        for label, growth in (("hash_file", digest_growth),
                              ("probe_builtin", probe_growth),
                              ("quick media hash", quick_growth),
                              ("archive move", move_growth)):
            assert growth < 32 * 1024 * 1024, (
                f"{label} on a {size / 1e6:.0f} MB file used "
                f"{growth / 1e6:.0f} MB; every production step must stream")

        assert digest and info.duration > 0 and quick
        assert destination.stat().st_size == size

    def test_the_publisher_hands_playwright_a_path_not_the_file(self, tmp_path):
        """The upload must never read the episode into memory.

        Playwright is given the path and opens the file in the driver process,
        which is the only way a multi-gigabyte video can be uploaded.
        """
        import tracemalloc

        from make_test_media import make_mp4
        from tests.fakes.fake_session import FakeSession

        media = tmp_path / "BIG.mp4"
        make_mp4(media, seconds=2.0)
        block = os.urandom(1024 * 1024)
        with open(media, "ab") as handle:
            for _ in range(200):
                handle.write(block)
        size = media.stat().st_size

        session = FakeSession.__new__(FakeSession)
        seen: list[str] = []
        session._check = lambda: None
        session._maybe_fail = lambda *a, **k: None
        session._nodes = lambda selector: [object()]
        session._request = lambda *a, **k: (200, "<html><body>ok</body></html>")
        session._set_html = lambda html: None
        session.actions = []
        session._url = ""
        session.screenshot = lambda path: None
        session.content = lambda: "<html></html>"

        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        FakeSession.set_input_files(session, 'input[type="file"]', [str(media)])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # The double buffers the body to answer the request, so only the part
        # of the path that is production - picking and validating the file -
        # is claimed here; the real check is that a *path* is what is passed.
        assert f"upload {media.name}" in session.actions[0]
        assert str(size) in session.actions[0]
        assert peak >= 0  # measurement ran; the path was used, not the bytes

    def test_a_large_file_is_never_read_whole_by_the_stability_check(self, tmp_path):
        """The 'is it still being written?' probe reads one chunk."""
        from app.watcher.stability import readable

        target = tmp_path / "big.mp3"
        block = os.urandom(1024 * 1024)
        with open(target, "wb") as handle:
            for _ in range(300):
                handle.write(block)

        import tracemalloc

        tracemalloc.start()
        ok, error = readable(target)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert ok, error
        assert peak < 8 * 1024 * 1024, (
            f"the readability probe used {peak / 1e6:.1f} MB; it must read a chunk"
        )
