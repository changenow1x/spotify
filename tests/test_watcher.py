"""File-stability detection and media/metadata pairing.

The watcher must never hand a half-copied file to the uploader, and must
never invent a pairing the user did not make.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from app.core.hashing import content_hash, hash_file, hash_path, hash_text, safe_digest
from app.watcher.pairing import (
    base_name_of,
    find_pair,
    is_media_name,
    is_metadata_name,
    pair_directory,
    scan_directory,
)
from app.watcher.stability import (
    FileStability,
    StabilityTimeout,
    StabilityTracker,
    is_file_locked,
    readable,
    wait_until_stable,
)


# ------------------------------------------------------------- stability

class TestStabilityTracker:
    def _tracker(self, **kw):
        kw.setdefault("stable_seconds", 0.1)
        kw.setdefault("poll_interval", 0.01)
        kw.setdefault("stable_checks", 1)
        return StabilityTracker(**kw)

    def test_new_file_is_not_stable_immediately(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = self._tracker(stable_seconds=0.3)
        state = t.track(p)
        assert t.is_stable(state) is False
        assert t.reason_not_stable(state)

    def test_unchanged_size_becomes_stable(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x" * 10)
        t = self._tracker(stable_seconds=0.05)
        # The first observation starts the clock; the second confirms it.
        t.poll(p)
        time.sleep(0.15)
        state = t.poll(p)
        assert t.is_stable(state) is True
        assert t.reason_not_stable(state) == ""
        assert state.stable_since is not None
        assert state.consecutive == 2

    def test_a_short_burst_must_repeat_before_it_counts(self, tmp_path):
        """One unchanged sample is not enough; stable_checks guards a pause."""
        p = tmp_path / "a.bin"
        p.write_bytes(b"x" * 10)
        t = StabilityTracker(stable_seconds=0.0, poll_interval=0.01, stable_checks=3)
        state = t.track(p)
        assert t.is_stable(t.poll(p)) is False
        assert t.is_stable(t.poll(p)) is False
        assert t.is_stable(t.poll(p)) is True

    def test_growing_file_never_settles(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = self._tracker(stable_seconds=0.05)
        stop = threading.Event()

        def grower():
            while not stop.is_set():
                with open(p, "ab") as fh:
                    fh.write(b"0" * 64)
                time.sleep(0.02)

        thread = threading.Thread(target=grower, daemon=True)
        state = t.track(p)
        thread.start()
        try:
            for _ in range(25):
                if t.is_stable(t.poll(p)):
                    break
                time.sleep(0.02)
            assert t.is_stable(t.poll(p)) is False
        finally:
            stop.set()
            thread.join(timeout=2)

    def test_shrinking_file_is_not_stable(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x" * 100)
        t = self._tracker(stable_seconds=0.05)
        state = t.track(p)
        p.write_bytes(b"x" * 10)
        state = t.poll(p)
        assert t.is_stable(state) is False

    def test_deleted_file(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = self._tracker()
        state = t.track(p)
        p.unlink()
        state = t.poll(p)
        assert state.exists is False
        assert t.is_stable(state) is False

    def test_tracked_and_forget(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = self._tracker()
        t.track(p)
        assert [str(s.path) for s in t.tracked()] == [str(p)]
        t.forget(p)
        assert t.tracked() == []

    def test_clear_stable_restarts_the_clock(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = self._tracker(stable_seconds=0.05)
        t.poll(p)
        t.poll(p)
        t.clear_stable(p)
        state = t.poll(p)
        assert state.consecutive == 1
        assert t.is_stable(state) is False

    def test_expired_after_the_timeout(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = StabilityTracker(stable_seconds=0.01, poll_interval=0.05, timeout=5.0)
        state = t.track(p)
        assert t.expired(state) is False
        state.first_seen -= 60
        assert t.expired(state) is True

    def test_timings_are_clamped_to_sane_minimums(self):
        """Sub-second timeouts would spam the dashboard with false alarms."""
        t = StabilityTracker(stable_seconds=-5, poll_interval=0.0001,
                             stable_checks=0, timeout=0.001)
        assert t.stable_seconds == 0.0
        assert t.poll_interval >= 0.05
        assert t.stable_checks >= 1
        assert t.timeout >= 1.0

    def test_state_is_serialisable(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        t = self._tracker()
        data = t.poll(p).as_dict()
        assert isinstance(data, dict)
        assert data["name"] == "a.bin"
        assert data["size"] == 1
        assert data["locked"] is False and data["read_error"] == ""
        assert "stable_for" in data


class TestWaitUntilStable:
    def test_returns_when_the_file_settles(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x" * 100)
        result = wait_until_stable(p, stable_seconds=0.05, stable_checks=1,
                                   poll_interval=0.01, timeout=5)
        assert t_is_stable(result)
        assert str(result.path) == str(p)

    def test_timeout_raises_with_a_useful_message(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        with pytest.raises(StabilityTimeout) as exc:
            wait_until_stable(p, stable_seconds=300, poll_interval=0.01, timeout=0.1)
        message = str(exc.value)
        assert "changing" in message.lower() or "stable" in message.lower()
        assert "a.bin" in message

    def test_should_continue_cancels_the_wait(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        with pytest.raises(StabilityTimeout):
            wait_until_stable(p, stable_seconds=300, poll_interval=0.01, timeout=10,
                              should_continue=lambda: False)

    def test_progress_callback_fires(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x" * 10)
        seen: list[FileStability] = []
        wait_until_stable(p, stable_seconds=0.1, stable_checks=1, poll_interval=0.02,
                          timeout=5, on_wait=seen.append)
        assert seen
        assert t_is_stable(seen[-1])

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises((StabilityTimeout, FileNotFoundError, OSError)):
            wait_until_stable(tmp_path / "nope.bin", stable_seconds=0.05,
                              poll_interval=0.01, timeout=0.2)


def t_is_stable(result: FileStability) -> bool:
    return result.stable_since is not None and result.read_error == ""


class TestReadability:
    def test_readable_file(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        ok, reason = readable(p)
        assert ok is True and reason == ""

    def test_missing_file(self, tmp_path):
        ok, reason = readable(tmp_path / "nope.bin")
        assert ok is False and reason

    def test_unreadable_file(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        p.chmod(0o000)
        try:
            ok, reason = readable(p)
            if os.geteuid() != 0:  # root can read anything
                assert ok is False and reason
        finally:
            p.chmod(0o600)

    def test_lock_detection_does_not_raise(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"x")
        assert isinstance(is_file_locked(p), bool)


# --------------------------------------------------------------- pairing

class TestNameClassification:
    @pytest.mark.parametrize("name,kind", [
        ("ep.mp4", "video"), ("ep.mov", "video"), ("ep.mpg", "video"),
        ("ep.mp3", "audio"), ("ep.m4a", "audio"), ("ep.wav", "audio"),
    ])
    def test_media(self, name, kind, config):
        ok, detected = is_media_name(name, config.media)
        assert ok is True and detected.value == kind

    @pytest.mark.parametrize("name", ["ep.txt", "EP.TXT", "ep.json", "ep.jpg"])
    def test_not_media(self, name, config):
        assert is_media_name(name, config.media)[0] is False

    def test_metadata_names(self, config):
        assert is_metadata_name("ep.txt", config.media) is True
        assert is_metadata_name("ep.mp4", config.media) is False

    def test_base_name_strips_only_the_last_extension(self):
        assert base_name_of("My Show - S01E02.mp4") == "My Show - S01E02"
        assert base_name_of("/a/b/c.name.txt") == "c.name"
        assert base_name_of(Path("x/y.mp3")) == "y"

    def test_unknown_extension_is_not_media(self, config):
        assert is_media_name("ep.xyz", config.media)[0] is False


class TestScanDirectory:
    def test_splits_media_and_metadata(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x" * 10)
        (tmp_path / "ep01.txt").write_text("TITLE=Ep 1\n", encoding="utf-8")
        (tmp_path / "readme.md").write_text("hi", encoding="utf-8")
        (tmp_path / "cover.jpg").write_bytes(b"x")

        media, metadata = scan_directory(tmp_path, policy=config.media)

        assert [m.base_name for m in media] == ["ep01"]
        assert [m.path.name for m in metadata] == ["ep01.txt"]
        assert media[0].kind.value == "video"
        assert media[0].size == 10

    def test_ignores_partial_downloads_and_lock_files(self, tmp_path, config):
        (tmp_path / "ep01.mp4.part").write_bytes(b"x")
        (tmp_path / "ep01.mp4.crdownload").write_bytes(b"x")
        (tmp_path / ".ep01.mp4.swp").write_bytes(b"x")
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        media, _ = scan_directory(tmp_path, policy=config.media)
        assert [m.base_name for m in media] == ["ep01"]

    def test_subdirectories_are_not_scanned(self, tmp_path, config):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "ep02.mp4").write_bytes(b"x")
        media, _ = scan_directory(tmp_path, policy=config.media)
        assert media == []

    def test_missing_directory(self, tmp_path, config):
        media, metadata = scan_directory(tmp_path / "gone", policy=config.media)
        assert media == [] and metadata == []


class TestFindPair:
    def test_exact_base_name_match(self, tmp_path, config):
        from app.core.models import MediaKind

        (tmp_path / "ep01.mp4").write_bytes(b"x")
        (tmp_path / "ep01.txt").write_text("TITLE=T\n", encoding="utf-8")
        media, metadata = scan_directory(tmp_path, policy=config.media)
        match, reasons = find_pair(media[0], metadata)
        assert match is not None and match.path.name == "ep01.txt"
        assert reasons == []

    def test_case_insensitive_match(self, tmp_path, config):
        (tmp_path / "EP01.mp4").write_bytes(b"x")
        (tmp_path / "ep01.txt").write_text("TITLE=T\n", encoding="utf-8")
        media, metadata = scan_directory(tmp_path, policy=config.media)
        match, _ = find_pair(media[0], metadata)
        assert match is not None

    def test_no_match_reports_a_reason(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        (tmp_path / "other.txt").write_text("TITLE=T\n", encoding="utf-8")
        media, metadata = scan_directory(tmp_path, policy=config.media)
        match, reasons = find_pair(media[0], metadata)
        assert match is None
        assert reasons and isinstance(reasons[0], str)

    def test_never_guesses_between_two_case_variants(self, tmp_path, config):
        """Two files that fold to the same name are ambiguous: no match."""
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        (tmp_path / "EP01.txt").write_text("TITLE=A\n", encoding="utf-8")
        (tmp_path / "Ep01.txt").write_text("TITLE=B\n", encoding="utf-8")
        media, metadata = scan_directory(tmp_path, policy=config.media)
        match, reasons = find_pair(media[0], metadata)
        assert match is None
        assert any("could match" in r.lower() or "rename" in r.lower()
                   for r in reasons)

    def test_single_case_variant_matches_but_warns(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        (tmp_path / "EP01.txt").write_text("TITLE=A\n", encoding="utf-8")
        media, metadata = scan_directory(tmp_path, policy=config.media)
        match, reasons = find_pair(media[0], metadata)
        assert match is not None
        assert any("case-insensitively" in r for r in reasons)


class TestPairDirectory:
    def test_paired_default(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        (tmp_path / "ep01.txt").write_text("TITLE=T\n", encoding="utf-8")
        pairs, orphans, reasons = pair_directory(
            tmp_path, policy=config.media, show_id="test-show")
        assert len(pairs) == 1
        assert pairs[0].job_type == "paired"
        assert pairs[0].media.base_name == "ep01"
        assert pairs[0].metadata is not None
        assert pairs[0].show_id == "test-show"
        assert orphans == []

    def test_media_only_is_refused_by_default(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        pairs, orphans, reasons = pair_directory(tmp_path, policy=config.media)
        assert pairs == []
        assert [c.base_name for c in orphans] == ["ep01"]

    def test_media_only_is_allowed_when_enabled(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        pairs, orphans, _ = pair_directory(tmp_path, policy=config.media,
                                           allow_media_only=True)
        assert len(pairs) == 1
        assert pairs[0].job_type == "media_only"
        assert pairs[0].metadata is None
        assert orphans == []

    def test_lone_metadata_is_never_paired(self, tmp_path, config):
        (tmp_path / "ep01.txt").write_text("TITLE=T\n", encoding="utf-8")
        pairs, orphans, reasons = pair_directory(
            tmp_path, policy=config.media, allow_media_only=True)
        assert pairs == []
        assert orphans == []

    def test_pair_is_serialisable(self, tmp_path, config):
        (tmp_path / "ep01.mp4").write_bytes(b"x")
        (tmp_path / "ep01.txt").write_text("TITLE=T\n", encoding="utf-8")
        pairs, _, _ = pair_directory(tmp_path, policy=config.media)
        data = pairs[0].as_dict()
        assert data["base_name"] == "ep01"
        assert data["media"]["name"] == "ep01.mp4"
        assert data["media"]["kind"] == "video"
        assert data["metadata"]["name"] == "ep01.txt"
        assert pairs[0].metadata_path.name == "ep01.txt"
        assert pairs[0].media_path.name == "ep01.mp4"


# --------------------------------------------------------------- hashing

class TestHashing:
    def test_text_hash_is_stable(self):
        assert hash_text("abc") == hash_text("abc")
        assert hash_text("abc") != hash_text("abd")

    def test_file_hash_matches_bytes(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"hello world")
        assert hash_file(p) == hash_file(p)

    def test_identical_content_different_names_match(self, tmp_path):
        a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
        a.write_bytes(b"same bytes")
        b.write_bytes(b"same bytes")
        assert hash_file(a) == hash_file(b)

    def test_content_hash_combines_both_halves(self):
        a = content_hash("media-hash", "meta-hash")
        assert a != content_hash("media-hash", "other")
        assert a != content_hash("other", "meta-hash")
        assert a == content_hash("media-hash", "meta-hash")

    def test_content_hash_tolerates_missing_half(self):
        assert content_hash(None, None)
        assert content_hash("x", None) == content_hash("x", None)
        assert content_hash("x", None) != content_hash(None, None)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            hash_file(tmp_path / "nope.bin")

    def test_quick_mode_is_available(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"z" * 2048)
        result = hash_path(p, mode="quick")
        assert result.as_dict()["size"] == 2048
        assert result.digest

    def test_safe_digest_is_a_short_display_abbreviation(self):
        assert safe_digest(None) == "-"
        assert safe_digest("") == "-"
        assert safe_digest("a" * 64) == "a" * 12
