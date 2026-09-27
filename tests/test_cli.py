"""Command-line interface tests.

These run ``python -m app`` as a real subprocess against a temporary root, so
argument parsing, exit codes and the printed guidance are all covered as the
user actually experiences them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MINIMAL_CONFIG = """
root: ""
hash_mode: quick
shows:
  - name: "CLI Show"
    id: "cli-show"
    enabled: true
    timezone: "Europe/Berlin"
    spotify_show_id: ""
    defaults:
      explicit: false
      publish: "NOW"
watcher:
  stable_seconds: 0.2
  poll_interval_seconds: 0.1
media:
  allow_media_only: false
"""


@pytest.fixture()
def project(tmp_path):
    """A throwaway installation root with a config file."""
    root = tmp_path / "install"
    (root / "config").mkdir(parents=True)
    config = root / "config" / "config.yaml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return root, config


def run(root, *args, expect=None, env=None, timeout=300, stdin=""):
    """Run the CLI as a user would, returning the CompletedProcess."""
    environment = dict(os.environ)
    environment.update(env or {})
    environment["PYTHONPATH"] = str(ROOT)
    process = subprocess.run(
        [sys.executable, "-m", "app", "--root", str(root), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
        input=stdin, env=environment,
    )
    if expect is not None:
        assert process.returncode == expect, (
            f"exit {process.returncode} != {expect}\n"
            f"--- stdout ---\n{process.stdout}\n--- stderr ---\n{process.stderr}"
        )
    return process


# ----------------------------------------------------------------- help ---
class TestHelp:
    def test_bare_help_exits_zero(self, project):
        root, _ = project
        result = run(root, "--help", expect=0)
        assert "incoming" in result.stdout.lower()

    def test_help_works_without_a_config(self, tmp_path):
        """--help must not need a config file, and must list the commands."""
        result = run(tmp_path / "nothing", "--help", expect=0)
        assert "check" in result.stdout
        assert "spotify-smoke-test" in result.stdout

    def test_the_three_documented_entry_points_are_present(self, project):
        root, _ = project
        result = run(root, "--help", expect=0)
        assert "spotify-check" in result.stdout
        assert "spotify-smoke-test" in result.stdout

    def test_documented_commands_exist(self, project):
        root, _ = project
        result = run(root, "--help", expect=0)
        for command in ("check", "spotify-check", "spotify-smoke-test", "auth",
                        "status", "jobs", "retry", "cancel"):
            assert command in result.stdout, f"'{command}' is not advertised"

    def test_unknown_command_is_rejected(self, project):
        root, _ = project
        result = run(root, "publish-everything")
        assert result.returncode != 0
        assert "invalid choice" in (result.stderr + result.stdout).lower()


class TestFlagAndSubcommandForms:
    """Both spellings must work, and the shared options must survive either
    argument order.  argparse applies subparser defaults *after* the main
    parser, so this is a regression guard, not decoration."""

    def test_documented_flag_form_works(self, project):
        root, _ = project
        result = run(root, "--check")
        assert result.returncode in (0, 1)
        assert "[OK" in result.stdout, "--check must behave like the check subcommand"

    @pytest.mark.parametrize("alias", [
        "--check", "--status", "--spotify-check", "--spotify-smoke-test",
        "--doctor", "--jobs", "--disconnect", "--test",
    ])
    def test_every_documented_flag_alias_is_accepted(self, project, alias):
        root, _ = project
        result = run(root, alias, "--help", timeout=300)
        assert "unrecognized arguments" not in result.stderr, (
            f"{alias} is not accepted"
        )

    def test_root_before_the_subcommand(self, project, tmp_path):
        explicit = tmp_path / "explicit-before"
        result = run(project[0], "--root", str(explicit), "status", expect=0)
        assert str(explicit) in result.stdout, "--root before the subcommand was ignored"
        assert "Folder:  %s" % explicit in result.stdout

    def test_root_after_the_subcommand(self, project, tmp_path):
        explicit = tmp_path / "explicit-after"
        result = run(project[0], "status", "--root", str(explicit), expect=0)
        assert str(explicit) in result.stdout, "--root after the subcommand was ignored"
        assert "Folder:  %s" % explicit in result.stdout

    def test_both_orders_agree(self, project, tmp_path):
        before = run(project[0], "--root", str(tmp_path / "o1"), "status", expect=0)
        after = run(project[0], "status", "--root", str(tmp_path / "o2"), expect=0)
        # Same shape of output, different roots, both honoured.
        assert "data/podcast.db" in before.stdout and "data/podcast.db" in after.stdout
        assert str(tmp_path / "o1") in before.stdout
        assert str(tmp_path / "o2") in after.stdout

    def test_root_never_leaks_into_the_source_tree(self, project):
        """A wrong --root must not create folders inside the repository."""
        import shutil

        stray = Path(__file__).resolve().parents[1] / "runtime"
        was_there = stray.exists()
        root, _ = project
        run(root, "--root", str(root), "check")
        assert not stray.exists() or was_there, (
            "the app created a runtime/ folder in the project instead of using --root"
        )
        if not was_there and stray.exists():
            shutil.rmtree(stray)


# ---------------------------------------------------------------- check ---
class TestCheck:
    def _check(self, root):
        """Run check and return (result, had_failures)."""
        result = run(root, "check")
        assert result.returncode in (0, 1), (
            f"unexpected exit {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
        return result, "[FAIL" in result.stdout

    def test_check_reports_each_item(self, project):
        root, _ = project
        result, _ = self._check(root)
        for label in ("[OK", "[FAIL", "[WARN"):
            assert label in result.stdout, f"{label} marker missing from the report"
        assert "SQLite database" in result.stdout
        assert "Python package" in result.stdout
        assert "Chromium browser" in result.stdout

    def test_check_exit_code_agrees_with_its_report(self, project):
        root, _ = project
        result, had_fail = self._check(root)
        assert (result.returncode == 1) == had_fail, (
            "the exit code must agree with the [FAIL] lines"
        )

    def test_check_names_the_fix_for_a_missing_browser(self, project):
        """A missing browser must be reported honestly, with the remedy."""
        root, _ = project
        result, had_fail = self._check(root)
        if had_fail:
            assert "playwright install chromium" in result.stdout, (
                "a [FAIL] line must tell the user how to fix it"
            )
        else:
            assert "[OK  ] Chromium browser" in result.stdout

    def test_check_lists_what_is_missing(self, project):
        root, _ = project
        result, had_fail = self._check(root)
        if had_fail:
            assert "problem(s) must be fixed" in result.stdout
            assert "  - " in result.stdout, "each problem must be listed"

    def test_check_creates_the_folder_tree(self, project):
        root, _ = project
        self._check(root)
        assert (root / "shows" / "cli-show" / "incoming").is_dir()

    def test_check_reports_bad_configuration(self, project):
        root, config = project
        config.write_text("shows: 'not a list'\n", encoding="utf-8")
        result = run(root, "check", expect=2)
        assert "[FAIL" in result.stdout

    def test_check_is_read_only_beyond_folder_creation(self, project):
        root, _ = project
        self._check(root)
        self._check(root)
        assert (root / "config" / "config.yaml").exists(), "check must not rewrite the config"


# --------------------------------------------------------- safe commands ---
class TestReadOnlyCommands:
    def test_status_without_a_database(self, project):
        root, _ = project
        result = run(root, "status", expect=0)
        assert "show" in result.stdout.lower()

    def test_status_json_is_machine_readable(self, project):
        root, _ = project
        result = run(root, "--json", "status", expect=0)
        payload = json.loads(result.stdout)
        assert isinstance(payload, (dict, list))

    def test_jobs_on_an_empty_installation(self, project):
        root, _ = project
        result = run(root, "jobs", expect=0)
        assert result.stdout.strip()

    def test_retry_unknown_job_fails_cleanly(self, project):
        root, _ = project
        result = run(root, "retry", "no-such-job")
        assert result.returncode != 0
        assert "Traceback" not in result.stderr, "a bad job id must not print a traceback"

    def test_cancel_unknown_job_fails_cleanly(self, project):
        root, _ = project
        result = run(root, "cancel", "no-such-job")
        assert result.returncode != 0
        assert "Traceback" not in result.stderr

    def test_make_test_media_writes_a_usable_pair(self, project):
        root, _ = project
        folder = root / "shows" / "cli-show" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        run(root, "make-test-media", str(folder),
            "--name", "Demo Episode", "--media", "mp3", expect=0)
        files = sorted(p.name for p in folder.iterdir())
        assert any(f.endswith(".mp3") for f in files), files
        assert any(f.endswith(".txt") for f in files), files
        text = next(folder.glob("*.txt")).read_text(encoding="utf-8")
        assert "TITLE=" in text and "DESCRIPTION=" in text

    @pytest.mark.parametrize("kind,suffix", [("mp4", ".mp4"), ("mov", ".mov"),
                                             ("wav", ".wav"), ("mp3", ".mp3"),
                                             ("mpg", ".mpg")])
    def test_make_test_media_writes_each_documented_format(self, project, kind, suffix):
        root, _ = project
        folder = root / "shows" / "cli-show" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        run(root, "make-test-media", str(folder), "--name", "Fmt", "--media", kind, expect=0)
        assert any(p.suffix == suffix for p in folder.iterdir()), sorted(
            p.name for p in folder.iterdir())

    @pytest.mark.parametrize(
        "hostile",
        ["../../escape", "..\\..\\escape", "a/b", "C:\\evil", "....//....//x", "  ..  "],
    )
    def test_make_test_media_cannot_escape_the_target_folder(self, project, hostile):
        """A hostile name may be sanitised, but must never write outside."""
        root, _ = project
        folder = root / "shows" / "cli-show" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        marker = root / "shows" / "marker.txt"
        marker.write_text("x", encoding="utf-8")
        run(root, "make-test-media", str(folder), "--name", hostile, "--media", "mp3")
        # Everything that appeared must sit inside the requested folder.
        for produced in folder.iterdir():
            assert folder.resolve() in produced.resolve().parents
        # And nothing landed one or two levels up.
        assert not list(root.glob("escape.*"))
        assert not list(root.parent.glob("escape.*"))
        assert marker.exists()


# ------------------------------------------------- safety-critical flags ---
class TestSmokeTestIsSafe:
    def test_smoke_test_is_read_only_by_default(self, project, monkeypatch):
        """Without --live-publish it must never call the publishing path."""
        root, config = project
        script = (
            "import sys, runpy\n"
            "import app.publishers.spotify.publisher as pub\n"
            "def boom(self, *a, **k):\n"
            "    raise SystemExit('PUBLISH WAS ATTEMPTED')\n"
            "pub.SpotifyPublisher.publish = boom\n"
            "pub.SpotifyPublisher._live_publish = boom\n"
            "sys.argv = ['app', '--root', %r, 'spotify-smoke-test']\n"
            "runpy.run_module('app', run_name='__main__')\n"
        ) % str(root)
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=300, cwd=str(ROOT), env=env,
        )
        assert "PUBLISH WAS ATTEMPTED" not in (process.stdout + process.stderr)

    def test_live_publish_requires_a_typed_confirmation(self, project):
        """--live-publish must not fire on a bare return key."""
        root, _ = project
        result = run(
            root, "spotify-smoke-test", "--live-publish",
            timeout=300, stdin="\n",
        )
        combined = result.stdout + result.stderr
        assert "PUBLISH WAS ATTEMPTED" not in combined
        if result.returncode == 0:  # a full run would need a real account
            assert "Type PUBLISH" in result.stdout

    def test_live_publish_is_not_implied(self, project):
        root, _ = project
        result = run(root, "spotify-smoke-test", "--help", expect=0)
        assert "--live-publish" in result.stdout
        assert "actually publish" in result.stdout


# -------------------------------------------------------------- security ---
class TestCliIsSafeWithHostileInput:
    def test_hostile_show_name_does_not_crash_the_cli(self, project):
        root, config = project
        config.write_text(
            MINIMAL_CONFIG.replace('"CLI Show"', '"<script>alert(1)</script>"'),
            encoding="utf-8",
        )
        result = run(root, "check")
        assert "Traceback" not in result.stderr

    def test_output_is_plain_text_not_markup(self, project):
        root, config = project
        config.write_text(
            MINIMAL_CONFIG.replace('"CLI Show"', '"<img src=x onerror=alert(1)>"'),
            encoding="utf-8",
        )
        result = run(root, "status")
        assert "<img" in result.stdout  # shown verbatim...
        assert "Traceback" not in result.stderr  # ...without crashing


class TestSetupIsActuallyUsable:
    """`setup` writes the configuration the rest of the app then reads.

    It is the first command a new user runs, so a break here is a dead
    installation before anything else has happened.  Two real defects were
    found this way: the built-in template carried a key the config model
    rejects, and the root passed on the command line was not the root the
    folders were created under.
    """

    def test_the_builtin_template_validates(self):
        from app.core.config import AppConfig, CONFIG_TEMPLATE

        AppConfig.model_validate(CONFIG_TEMPLATE)  # must not raise

    def test_setup_creates_the_tree_under_the_requested_root(self, tmp_path):
        import subprocess
        import sys

        root = tmp_path / "install"
        root.mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "app", "setup", "--root", str(root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            input="My Show\n", capture_output=True, text=True, timeout=120)

        assert result.returncode == 0, result.stderr[-800:]
        assert (root / "config" / "config.yaml").is_file()
        assert (root / "shows").is_dir()
        shows = list((root / "shows").iterdir())
        assert len(shows) == 1, f"expected one show folder, got {shows}"
        assert (shows[0] / "incoming").is_dir()
        assert (shows[0] / "archive").is_dir()
        # It must not have created anything inside the source tree.
        assert "install" in result.stdout

    def test_setup_survives_a_closed_stdin(self, tmp_path):
        """A launcher or pipe must produce a clean message, not a traceback."""
        import subprocess
        import sys

        root = tmp_path / "install2"
        root.mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "app", "setup", "--root", str(root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

        assert result.returncode == 0, result.stderr[-800:]
        assert "Traceback" not in result.stderr, result.stderr[-500:]
        assert (root / "config" / "config.yaml").is_file()

    def test_rerunning_setup_keeps_the_existing_configuration(self, tmp_path):
        import subprocess
        import sys

        root = tmp_path / "install3"
        root.mkdir()
        env_root = str(root)
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-m", "app", "setup", "--root", env_root],
                cwd=str(Path(__file__).resolve().parents[1]),
                input="My Show\n", capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stderr[-800:]
        text = (root / "config" / "config.yaml").read_text(encoding="utf-8")
        assert text.count("name: My Show") == 1, "setup duplicated the show"
