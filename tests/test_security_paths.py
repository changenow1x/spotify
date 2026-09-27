"""Path-traversal and untrusted-input protection."""

from __future__ import annotations

import pytest

from app.security.paths import (
    MAX_FILENAME_LEN,
    PathSafetyError,
    is_within,
    safe_join,
    sanitize_filename,
    sanitize_stem,
    unique_path,
)


class TestSafeJoin:
    def test_joins_inside_root(self, tmp_path):
        assert safe_join(tmp_path, "a", "b.txt") == tmp_path / "a" / "b.txt"

    @pytest.mark.parametrize("evil", [
        "../etc/passwd",
        "../../windows/system32",
        "a/../../b",
        "..",
        "a/b/../../../c",
    ])
    def test_traversal_rejected(self, tmp_path, evil):
        with pytest.raises(PathSafetyError):
            safe_join(tmp_path, evil)

    @pytest.mark.parametrize("evil", [
        "/etc/passwd",
        "/",
        "C:\\Windows\\System32",
        "\\\\server\\share",
    ])
    def test_absolute_paths_rejected(self, tmp_path, evil):
        with pytest.raises(PathSafetyError):
            safe_join(tmp_path, evil)

    def test_nul_byte_rejected(self, tmp_path):
        with pytest.raises(PathSafetyError):
            safe_join(tmp_path, "a\x00b")

    def test_empty_rejected(self, tmp_path):
        with pytest.raises(PathSafetyError):
            safe_join(tmp_path, "")

    def test_none_rejected(self, tmp_path):
        with pytest.raises(PathSafetyError):
            safe_join(tmp_path, None)

    def test_shell_metacharacters_are_inert_but_allowed_as_data(self, tmp_path):
        # These are legitimate characters in a filename; they must never be
        # interpreted, and safe_join must not expand them.
        result = safe_join(tmp_path, "a; rm -rf ~ $(whoami) & echo | cat.txt")
        assert result.parent == tmp_path
        assert "$(whoami)" in result.name

    def test_is_within(self, tmp_path):
        assert is_within(tmp_path, tmp_path / "x")
        assert is_within(tmp_path, tmp_path)
        assert not is_within(tmp_path / "a", tmp_path / "b")


class TestSanitizeFilename:
    def test_preserves_german(self):
        assert sanitize_filename("Bewusstsein – Größe.txt") == "Bewusstsein – Größe.txt"

    def test_strips_path_separators(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename(r"..\..\windows\evil.txt") == "evil.txt"

    def test_replaces_illegal_chars(self):
        assert sanitize_filename('a<b>c:d"e|f?g*h.txt') == "a_b_c_d_e_f_g_h.txt"

    def test_strips_control_chars(self):
        assert sanitize_filename("a\x00b\x1fc.txt") == "a_b_c.txt"

    def test_reserved_device_names(self):
        assert sanitize_filename("CON") == "CON_"
        assert sanitize_filename("nul.txt") == "nul_.txt"  # Windows refuses "nul"
        assert sanitize_filename("COM1.mp4") == "COM1_.mp4"

    def test_trailing_dots_and_spaces_stripped(self):
        assert sanitize_filename("name...  ") == "name"

    def test_dotfiles_and_dotdot_replaced(self):
        assert sanitize_filename(".") == "unnamed"
        assert sanitize_filename("..") == "unnamed"
        assert sanitize_filename("") == "unnamed"

    def test_truncation_keeps_extension(self):
        long = "x" * 500 + ".mp4"
        out = sanitize_filename(long)
        assert len(out) <= MAX_FILENAME_LEN
        assert out.endswith(".mp4")

    def test_never_returns_a_separator(self):
        for evil in ["a/b", "a\\b", "../../x", "..\\..\\x", "/"]:
            assert "/" not in sanitize_filename(evil)
            assert "\\" not in sanitize_filename(evil)

    def test_sanitize_stem(self):
        assert sanitize_stem("THOT_Der_Schluessel") == "THOT_Der_Schluessel"
        assert sanitize_stem("../../etc/passwd") == "passwd"


class TestUniquePath:
    def test_increments(self, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("1")
        second = unique_path(target)
        assert second.name == "a (2).txt"
        second.write_text("2")
        assert unique_path(target).name == "a (3).txt"

    def test_returns_original_when_absent(self, tmp_path):
        assert unique_path(tmp_path / "new.txt") == tmp_path / "new.txt"


class TestArchivePathSafety:
    def test_metadata_cannot_redirect_the_archive(self, config, show):
        """A filename that is a traversal attempt cannot escape the archive."""
        from app.archive.manager import ArchiveManager
        from app.security.paths import safe_join

        archive_root = config.show_path(show, "archive")
        with pytest.raises(PathSafetyError):
            safe_join(archive_root, "../../../../Windows/System32/evil.mp4")

    def test_archive_manager_sanitizes_names(self, config, show, tmp_path):
        from app.archive.manager import ArchiveManager

        manager = ArchiveManager(config)
        # A file whose *name* is a traversal string; the real path is inside.
        weird = tmp_path / "incoming"
        weird.mkdir()
        media = weird / "..mp4"
        media.write_bytes(b"x" * 32)
        meta = weird / "..txt"
        meta.write_text("TITLE=x\nDESCRIPTION=y", encoding="utf-8")

        result = manager.archive(show, media, meta)
        assert result.ok
        assert result.destination_dir is not None
        assert is_within(config.show_path(show, "archive"), result.media_path)
        assert is_within(result.destination_dir, result.media_path)


class TestUntrustedFilenamesEndToEnd:
    def test_malicious_filename_never_escapes(self, config, show, tmp_path):
        """A hostile drop name is sanitised for disk but recorded verbatim."""
        from app.core.service import AutomationService
        from app.watcher.pairing import EpisodePair, MediaCandidate, MetadataCandidate
        from app.core.models import MediaKind

        service = AutomationService(config, ensure_dirs=False)
        incoming = config.show_incoming(show)
        incoming.mkdir(parents=True, exist_ok=True)
        # A legal filename that is maximally hostile: shell metacharacters,
        # spaces, quotes, leading dots and a Unicode homoglyph.
        name = "..evil'; rm -rf ~ $(whoami) & echo | cat – ü.mp4"
        media_path = incoming / name
        media_path.write_bytes(b"\x00" * 4096)
        meta_path = incoming / (name[:-4] + ".txt")
        meta_path.write_text("TITLE=Safe\nDESCRIPTION=Sicher", encoding="utf-8")

        pair = EpisodePair(
            media=MediaCandidate(media_path, name[:-4], ".mp4", 4096, MediaKind.VIDEO),
            metadata=MetadataCandidate(meta_path, name[:-4], ".txt", 10),
            show_id=show.resolved_id(),
        )
        job = service.on_pair_discovered(pair)
        assert job is not None
        # The original name is preserved for the user...
        assert job["original_filename"] == name
        # ...but the stored path is inside the incoming folder.
        assert is_within(incoming, job["media_path"])
        service.db.close()
