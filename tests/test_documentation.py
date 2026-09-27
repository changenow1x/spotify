"""The documentation must describe the application that actually exists.

Documentation drifts silently, and a user who follows a wrong instruction
loses an episode.  These tests read the shipped docs and check their claims
against the real code, so a wrong example fails the build instead of failing
the user.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.cli import FLAG_ALIASES, build_parser
from app.media.validator import SPOTIFY_LIMITS

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]


def _code_blocks(markdown: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", markdown, re.DOTALL)


# ---------------------------------------------------------------------------
# Every metadata example in the docs must be a file the parser accepts.
# ---------------------------------------------------------------------------


def _metadata_blocks() -> list[tuple[Path, str]]:
    """Complete metadata files documented anywhere in the docs.

    A block that only illustrates one field (``TITLE=...`` on its own) is not
    a file a user would write, so it is not treated as one.  A block counts
    as a file when it has the two required fields.
    """
    found = []
    for doc in DOCS:
        for block in _code_blocks(doc.read_text(encoding="utf-8")):
            if (re.search(r"^TITLE=", block, re.MULTILINE)
                    and re.search(r"^DESCRIPTION=", block, re.MULTILINE)):
                found.append((doc, block))
    return found


def test_the_documentation_contains_metadata_examples():
    assert _metadata_blocks(), "no metadata example found to check"


@pytest.mark.parametrize("doc,block", _metadata_blocks(),
                         ids=[f"{d.name}#{i}" for i, (d, _) in enumerate(_metadata_blocks())])
def test_every_documented_metadata_example_is_valid(doc: Path, block: str):
    from app.metadata.parser import build_metadata, parse_metadata_text
    from app.metadata.validator import validate_metadata

    try:
        meta = build_metadata(parse_metadata_text(block))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{doc.name} documents a file the parser rejects: {exc}\n\n{block}")
    report = validate_metadata(meta, min_chapters=3, min_chapter_gap_seconds=30)
    assert report.ok, (
        f"{doc.name} documents a file that fails validation: "
        f"{[e.as_dict() for e in report.errors]}\n\n{block}")


# ---------------------------------------------------------------------------
# Every command in the docs must exist.
# ---------------------------------------------------------------------------


def test_every_documented_command_exists():
    parser = build_parser()
    known: set[str] = set(FLAG_ALIASES)
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            known |= set(choices)
            # The subparser's own actions count as valid too.
            for sub in choices.values():
                for sub_action in getattr(sub, "_actions", []):
                    sub_choices = getattr(sub_action, "choices", None)
                    if isinstance(sub_choices, dict):
                        known |= set(sub_choices)
                    # A real flag on a real subcommand is a valid token, e.g.
                    # "python -m app --root D:\PodcastTwo check".
                    known |= {flag for flag in getattr(sub_action, "option_strings", [])
                              if flag.startswith("--")}

    unknown = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for match in re.finditer(r"python -m app (--[a-z\-]+|[a-z][a-z\-]*)", text):
            token = match.group(1)
            if token not in known:
                unknown.append(f"{doc.name}: python -m app {token}")
    assert not unknown, f"documented commands that do not exist: {sorted(set(unknown))}"


# ---------------------------------------------------------------------------
# The limits quoted in the docs must match the code.
# ---------------------------------------------------------------------------


def test_documented_media_limits_match_the_validator():
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "60 GB" in architecture
    assert "10 GB" in architecture
    assert "12 h" in architecture
    assert "16:9" in architecture
    # And the code agrees.
    assert SPOTIFY_LIMITS.MAX_SIZE_BYTES == 60 * 1024 ** 3
    assert SPOTIFY_LIMITS.RECOMMENDED_SIZE_BYTES == 10 * 1024 ** 3
    assert SPOTIFY_LIMITS.MAX_DURATION_SECONDS == 12 * 3600


def test_documented_audio_formats_are_exactly_what_the_validator_accepts():
    troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    # No format may be advertised that Spotify does not accept.
    assert "AAC" not in troubleshooting
    assert "MP3, M4A and WAV" in troubleshooting
    for container in ("mp3", "m4a", "wav"):
        assert container in SPOTIFY_LIMITS.SUPPORTED_AUDIO_CONTAINERS
    assert "aiff" not in SPOTIFY_LIMITS.SUPPORTED_AUDIO_CONTAINERS


# ---------------------------------------------------------------------------
# No stale test counts, and no dangling links.
# ---------------------------------------------------------------------------


def test_the_test_count_in_the_docs_is_not_stale():
    """No document may quote a count that is not what pytest collects."""
    collected = _collected_count()
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for match in re.finditer(r"\b(\d{3})\s+tests\b", text):
            assert int(match.group(1)) == collected, (
                f"{doc.name} says {match.group(1)} tests; pytest collects "
                f"{collected}. Re-run the suite and update it.")

    # The table in TESTING.md quotes the total in a form the pattern above
    # does not match, and that is exactly where a stale number hid.
    testing = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    for pattern in (r"\*\*Total\*\*\s*\|\s*\*\*(\d+)\*\*",
                    r"All (\d+) must pass"):
        match = re.search(pattern, testing)
        assert match, f"TESTING.md no longer states a total: {pattern}"
        assert int(match.group(1)) == collected, (
            f"TESTING.md says {match.group(1)}; pytest collects {collected}")


def test_the_per_file_counts_in_testing_md_add_up():
    """A wrong per-file number is as misleading as a wrong total."""
    collected = _collected_count()
    testing = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([a-z0-9_]+\.py)` \| (\d+) \|", testing, re.MULTILINE)
    assert rows, "the per-file table is missing"
    per_file = {}
    for name, count in rows:
        per_file[name] = int(count)

    actual: dict[str, int] = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        out = __import__("subprocess").run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(path)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        match = re.search(r"(\d+) tests? collected", out.stdout)
        if match:
            actual[path.name] = int(match.group(1))

    for name, count in per_file.items():
        assert name in actual, f"TESTING.md lists {name}, which does not exist"
        assert actual[name] == count, (
            f"TESTING.md says {name} has {count} tests; it has {actual[name]}")
    assert sum(per_file.values()) == collected, (
        f"the table sums to {sum(per_file.values())}; pytest collects {collected}")


def _collected_count() -> int:
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    match = re.search(r"(\d+) tests? collected", out.stdout)
    assert match, f"could not count tests:\n{out.stdout[-500:]}"
    return int(match.group(1))


def test_every_local_link_resolves():
    broken = []
    for doc in DOCS:
        for match in re.finditer(r"\[([^\]]+)\]\(([^)#\s]+)\)", doc.read_text(encoding="utf-8")):
            target = match.group(2)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (doc.parent / target).exists():
                broken.append(f"{doc.name} -> {target}")
    assert not broken, f"broken links: {broken}"


def test_every_documented_file_exists():
    """A doc must not reference a script or file that is not there."""
    for doc in DOCS:
        for match in re.finditer(r"`([A-Z][A-Z_]+\.(?:bat|md|py|yaml))`",
                                 doc.read_text(encoding="utf-8")):
            name = match.group(1)
            if name.endswith((".bat", ".yaml", ".py")):
                assert (ROOT / name).exists(), (
                    f"{doc.name} references {name}, which does not exist")
            else:
                assert (ROOT / "docs" / name).exists(), (
                    f"{doc.name} references docs/{name}, which does not exist")


def test_the_documented_root_forms_both_work(tmp_path):
    """README documents both placements of --root; both must really run."""
    root = tmp_path / "alt-root"
    for argv in (["--root", str(root), "check"], ["check", "--root", str(root)]):
        result = subprocess.run(
            [sys.executable, "-m", "app", *argv],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
        )
        assert result.returncode in (0, 1), result.stderr
        assert "Traceback" not in result.stderr, result.stderr
        # The override must be honoured, not silently dropped.
        assert str(root) in result.stdout, (argv, result.stdout[-2000:])


def test_root_is_documented_because_it_exists():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--root" in readme
