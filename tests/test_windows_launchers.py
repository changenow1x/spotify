"""Static validation of the Windows launchers.

These scripts CANNOT be executed here: this is Linux, and ``cmd.exe`` does not
exist.  Claiming they "passed" would be false, so what this file does is check
them the only honest way available - by reading them and asserting the
properties that make them safe and correct on Windows.

What is asserted:
  * no hardcoded user names or drive paths
  * every script resolves its own directory, so it works from any cwd
  * the venv Python is found through %~dp0, not a fixed location
  * the Python commands are ones that actually exist in app/cli.py
  * nothing deletes or overwrites user data
  * error handling is present and exit codes are propagated
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.cli import _expand_flag_aliases, build_parser

ROOT = Path(__file__).resolve().parents[1]
BAT_FILES = ["SETUP.bat", "START.bat", "STOP.bat", "RESTART.bat",
             "SIGN_IN.bat", "CHECK.bat", "CHECK_SPOTIFY.bat"]

#: A real Windows username, a real home directory, or a hardcoded drive.
FORBIDDEN_PATTERNS = [
    (r"[A-Za-z]:\\Users\\", "a hardcoded user directory"),
    (r"C:\\Users\\", "a hardcoded system drive path"),
    (r"C:\\podcast|C:\\Podcast", "a hardcoded install path"),
    (r"%USERNAME%", "the current user baked into a path"),
]

#: Commands that would destroy a user's files.
DESTRUCTIVE_PATTERNS = [
    (r"\bdel\s+/[sqSQ]\b", "recursive delete"),
    (r"\brd\s+/[sS]\b", "recursive directory delete"),
    (r"\brmdir\s+/[sS]\b", "recursive directory delete"),
    (r"\bformat\b", "format"),
    (r"\bcipher\b", "cipher wipe"),
    (r">\s*[^\s]*\.txt\b", "a file overwrite redirect"),
]


@pytest.mark.parametrize("name", BAT_FILES)
def test_the_script_exists(name: str):
    path = ROOT / name
    assert path.exists(), f"{name} is missing"
    assert path.stat().st_size > 0, f"{name} is empty"


@pytest.mark.parametrize("name", BAT_FILES)
def test_no_hardcoded_user_paths(name: str):
    text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
    for pattern, description in FORBIDDEN_PATTERNS:
        assert not re.search(pattern, text, re.IGNORECASE), (
            f"{name} contains {description}: "
            f"{re.search(pattern, text, re.IGNORECASE).group(0)!r}")


@pytest.mark.parametrize("name", BAT_FILES)
def test_no_destructive_commands(name: str):
    text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
    for pattern, description in DESTRUCTIVE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        assert not match, f"{name} appears to run a {description}: {match.group(0)!r}"


@pytest.mark.parametrize("name", BAT_FILES)
def test_every_script_anchors_itself_to_its_own_directory(name: str):
    """A launcher must work from any working directory."""
    text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
    if "cd /d" not in text and "%~dp0" not in text:
        # STOP.bat has nothing to locate - it only talks to a port.
        assert "for /f" in text or "taskkill" in text, (
            f"{name} neither anchors to %~dp0 nor addresses a running process")


def test_scripts_that_run_python_find_the_venv_relative_to_themselves():
    """The venv must be found through %~dp0, never an absolute path."""
    for name in BAT_FILES:
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        if "python" not in text.lower():
            continue
        assert "%PY%" in text or "python.exe" in text, (
            f"{name} runs python without resolving the venv")
        if "%PY%" in text:
            assert "%CD%\\.venv\\Scripts\\python.exe" in text, (
                f"{name} does not resolve the venv from its own directory")
        assert not re.search(r"python\.exe\s+\"?[A-Za-z]:\\", text), (
            f"{name} invokes python by absolute path")


@pytest.mark.parametrize("name", BAT_FILES)
def test_python_invocations_are_real_commands(name: str):
    """Every command a launcher runs must actually exist in the CLI.

    Both documented styles are supported by the app, and a launcher may use
    either one:
        python -m app --check        python -m app check
    """
    text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
    # Only real invocations, never the ones a script prints as instructions.
    found = re.findall(r'^\s*"?%PY%"?\s+-m\s+app(.*)$', text, re.MULTILINE)
    if not found:
        # A launcher that never runs Python must stop the service another
        # way, or it does nothing at all.
        assert "taskkill" in text or "Stop-Process" in text, (
            f"{name} runs neither python nor a stop command")
        return

    parser = build_parser()
    for invocation in found:
        tokens = invocation.split()
        if not tokens:
            # A bare `python -m app` starts the service; that is real.
            assert parser.parse_args(["run"]) is not None
            continue
        # The `--flag` style is expanded to its subcommand exactly as main()
        # does, so an unknown command is caught here rather than ignored.
        try:
            expanded = _expand_flag_aliases(tokens)
            parser.parse_args(expanded)
        except SystemExit:
            pytest.fail(f"{name} runs an unknown command: python -m app {invocation}")


def test_error_handling_is_present():
    """A missing environment must be reported, not crash with a raw error."""
    for name in ("SETUP.bat", "START.bat", "RESTART.bat"):
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        assert "if not exist" in text, f"{name} does not check the venv exists"
        assert "exit /b" in text, f"{name} does not return an exit code"
        assert "[FAIL]" in text or "[OK]" in text, f"{name} reports no outcome"


def test_stop_leaves_unfinished_work_recoverable():
    """Stopping must not delete files; the next start recovers."""
    text = (ROOT / "STOP.bat").read_text(encoding="utf-8", errors="replace")
    assert "del " not in text.lower(), "STOP.bat deletes something"
    assert "taskkill" in text or "Stop-Process" in text, (
        "STOP.bat does not actually stop the process")


def test_restart_stops_before_it_starts():
    """A restart that starts first would fail to take the port."""
    text = (ROOT / "RESTART.bat").read_text(encoding="utf-8", errors="replace")
    stop_at = text.index("STOP.bat")
    start_at = text.index("-m app")
    assert stop_at < start_at, "RESTART.bat starts before it stops"


def test_check_scripts_are_declared_read_only():
    """A check must not publish anything."""
    for name in ("CHECK.bat", "CHECK_SPOTIFY.bat"):
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        assert "--live-publish" not in text, f"{name} could publish"
        assert re.search(r"-m\s+app\s+(?:--)?(check|spotify-check|doctor)\b", text), (
            f"{name} does not run a read-only check")
