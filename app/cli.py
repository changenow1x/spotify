"""Command line interface.

``python -m app``                    run the automation (watcher + worker + dashboard)
``python -m app --setup``            first-time setup wizard
``python -m app --check``            environment and configuration check
``python -m app --test``             run the test suite
``python -m app --auth``             sign in to Spotify once
``python -m app --status``           print a text status summary
``python -m app --doctor [--live]``  diagnose the Spotify integration
``python -m app --spotify-smoke-test``  controlled live Spotify check
``python -m app --jobs``             list jobs
``python -m app --retry <id>``       retry a job
``python -m app --disconnect``       forget the saved Spotify session
``python -m app --dashboard``        just the dashboard
``python -m app --make-test-media``  write sample files into an incoming folder
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .core.config import (
    CONFIG_TEMPLATE,
    AppConfig,
    ConfigError,
    default_root,
    dump_config,
    find_config_file,
    load_config,
)
from .core.logging_setup import setup_logging
from .core.service import AutomationService

BANNER = r"""
  ____          _            _   _         _                      _
 |  _ \ ___   _| |_ ___ ___ | |_(_)_ __   __| | ___  _ __   ___  ___| | __ _ _ __
 | |_) / _ \ / _` |/ _ \/ _ \| __| | '_ \ / _` |/ _ \| '_ \ / _ \/ __| |/ _` | '_ \
 |  __/ (_) | (_| |  __/ (_) | |_| | |_) | (_| | (_) | | | |  __/\__ \ | (_| | | | |
 |_|   \___/ \__,_|\___|\___/ \__|_| .__/ \__, |\___/|_| |_|\___||___/\__|\__,_|_| |_|
                                         |_|                    by the PodcastAutomation team
""".strip("\n")


def _eprint(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def _print(*args) -> None:
    print(*args, flush=True)


# ---------------------------------------------------------------- loading --

def load(args) -> AppConfig:
    overrides: dict = {}
    if getattr(args, "port", None):
        overrides.setdefault("api", {})["port"] = args.port
    if getattr(args, "headless", False):
        overrides.setdefault("spotify", {})["headless"] = True
    if getattr(args, "config", None):
        path = Path(args.config).expanduser()
        if not path.is_file():
            raise ConfigError(f"No configuration file at {path}.")
        return load_config(path, root=getattr(args, "root", None), overrides=overrides)
    path = find_config_file(Path(args.root) if getattr(args, "root", None) else None)
    return load_config(path, root=getattr(args, "root", None), overrides=overrides)


def bootstrap_logging(config: AppConfig, *, quiet: bool = False, console: str | None = None):
    return setup_logging(
        log_dir=config.logs_dir(),
        level=config.logging.level,
        console_level=console or config.logging.console_level,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
        activity_file=config.logging.activity_file,
        retain_days=config.logging.retain_days,
        quiet=quiet,
    )


# ------------------------------------------------------------------ setup --

def _ask(prompt: str) -> str:
    """Ask a question, tolerating a closed or non-interactive stdin.

    ``setup`` is often run from a launcher or a pipe, and an EOFError traceback
    is a poor way to learn that.
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        _print("")
        return ""


def cmd_setup(args) -> int:
    _print(BANNER)
    _print("\nPodcastAutomation - first-time setup\n" + "=" * 40)
    root = Path(args.root).expanduser() if args.root else default_root()
    if not args.root:
        _print(f"\nWhere should everything live?\n  [{root}]")
        answer = _ask("Folder (press Enter to accept): ")
        root = Path(answer).expanduser() if answer else root

    config_path = root / "config" / "config.yaml"
    _print(f"\nConfiguration file: {config_path}")

    if config_path.exists() and not args.force:
        _print("A configuration already exists. Keeping it (use --force to overwrite).")
    else:
        template = json.loads(json.dumps(CONFIG_TEMPLATE))  # deep copy
        template["root"] = str(root)
        _print("\nWhat is your podcast called?")
        name = _ask("Show name [My Podcast]: ") or "My Podcast"
        template["shows"] = [{
            "name": name,
            "enabled": True,
            "timezone": "Europe/Berlin",
            "spotify_show_id": "",
            "spotify_show_name": "",
            "defaults": {"explicit": False, "publish": "NOW",
                         "timezone": "Europe/Berlin", "language": "de"},
        }]
        cfg = AppConfig.model_validate(template)
        dump_config(cfg, config_path)
        _print(f"Wrote {config_path}")

    # Re-read what is on disk rather than trusting the branch above: setup is
    # routinely re-run to repair an installation, and the file that is
    # actually there is the one that has to work.  The root is passed
    # explicitly because the written file deliberately omits it - a config
    # that travelled to a new machine should find its own root, not the one
    # the machine it was written on happened to use.
    config = load_config(config_path, root=root)
    created = config.ensure_directories()
    _print("\nCreated these folders:")
    for path in created:
        _print(f"  {path}")

    _print("\nNext steps")
    _print("  1. Install dependencies:      SETUP.bat      (Windows)")
    _print("  2. Sign in to Spotify:        python -m app --auth")
    _print("  3. Start the automation:      START.bat")
    incoming = config.show_incoming(config.shows[0]) if config.shows else root / "shows"
    _print(f"  4. Drop files into:           {incoming}")
    _print("\nDone.")
    return 0


# ------------------------------------------------------------------ check --

def _line(status: str, label: str, detail: str = "") -> None:
    _print(f"[{status:4}] {label}" + (f"\n         {detail}" if detail else ""))


def _probe_chromium(config) -> tuple[bool, str]:
    """Actually try to launch the browser rather than guessing."""
    from .publishers.spotify.publisher import build_session_options
    from .publishers.spotify.browser import PlaywrightSession

    try:
        session = PlaywrightSession(build_session_options(config))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    try:
        session.start()
        return True, "launched successfully"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def cmd_check(args) -> int:
    _print(BANNER)
    failures: list[str] = []
    warnings: list[str] = []

    _print("\nEnvironment check")
    _print("=" * 60)

    version = sys.version_info
    if version >= (3, 10):
        _line("OK", f"Python {version.major}.{version.minor}.{version.micro}")
    else:
        failures.append("Python 3.10 or newer is required.")
        _line("FAIL", f"Python {version.major}.{version.minor} is too old",
              "Install Python 3.10 or newer from python.org and tick "
              "'Add python.exe to PATH'.")

    try:
        config = load(args)
    except ConfigError as exc:
        _line("FAIL", "Configuration", str(exc))
        _print("\n" + "=" * 60)
        return 2
    _line("OK", "Configuration", str(config.source_path or "(defaults)"))

    # --- folders -------------------------------------------------------
    try:
        created = config.ensure_directories()
    except OSError as exc:
        failures.append(f"Cannot create the runtime folder: {exc}")
        _line("FAIL", "Folders", str(exc))
        created = []
    else:
        detail = str(config.root_path())
        if created:
            detail += f"  ({len(created)} folder(s) created)"
        _line("OK", "Folders", detail)

    for show in config.shows:
        incoming = config.show_incoming(show)
        if incoming.is_dir():
            _line("OK", f"Incoming folder: {show.name}", str(incoming))
        else:
            failures.append(f"The incoming folder for {show.name} is missing.")
            _line("FAIL", f"Incoming folder: {show.name}", str(incoming))

    # --- writable ------------------------------------------------------
    probe = config.root_path() / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _line("OK", "Folder is writable", str(config.root_path()))
    except OSError as exc:
        failures.append(f"The project folder is not writable: {exc}")
        _line("FAIL", "Folder is not writable", f"{config.root_path()} - {exc}")

    # --- database ------------------------------------------------------
    db = config.database_path()
    try:
        import sqlite3

        db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(db))
        connection.execute("CREATE TABLE IF NOT EXISTS _check (id INTEGER)")
        connection.execute("DROP TABLE IF EXISTS _check")
        connection.close()
        _line("OK", "SQLite database", str(db))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"The database cannot be used: {exc}")
        _line("FAIL", "SQLite database", f"{db} - {exc}")

    # --- python packages ----------------------------------------------
    for module, purpose, install in (
        ("playwright", "Browser automation for Spotify", "python -m pip install playwright"),
        ("fastapi", "Local dashboard", "python -m pip install fastapi uvicorn"),
        ("watchdog", "Folder watching", "python -m pip install watchdog"),
        ("yaml", "Configuration files", "python -m pip install PyYAML"),
        ("pydantic", "Configuration validation", "python -m pip install pydantic"),
    ):
        try:
            __import__(module)
            _line("OK", f"Python package: {module}", purpose)
        except ImportError:
            failures.append(f"{module} is missing.")
            _line("FAIL", f"Python package: {module}", f"Install with: {install}")

    # --- ffmpeg --------------------------------------------------------
    from .media.ffmpeg import FfmpegTools

    tools = FfmpegTools(config.media.ffprobe_path, config.media.ffmpeg_path)
    if tools.available():
        _line("OK", "FFmpeg (ffprobe)", f"{tools.ffprobe} - {tools.version()}")
    else:
        warnings.append(
            "ffprobe was not found. The built-in reader handles MP3, WAV, M4A "
            "and MP4; install FFmpeg for full inspection."
        )
        _line("WARN", "FFmpeg (ffprobe) not found",
              "Optional. Install it and set media.ffprobe_path, or add it to PATH. "
              "The built-in media reader is used instead.")

    # --- browser -------------------------------------------------------
    if not _module_present("playwright"):
        _line("FAIL", "Chromium browser", "Playwright is not installed.")
    else:
        launched, detail = _probe_chromium(config)
        if launched:
            _line("OK", f"Chromium browser ({config.spotify.browser})", detail)
        else:
            failures.append("The Chromium browser could not be started.")
            _line("FAIL", f"Chromium browser ({config.spotify.browser})", detail)
            _print("         Install it with:  python -m playwright install chromium")

    # --- selectors -----------------------------------------------------
    from .publishers.spotify.selectors import selector_names, validate_registry

    problems = validate_registry()
    if problems:
        failures.extend(problems)
        _line("FAIL", "Spotify selectors", f"{len(problems)} problem(s) found")
        for problem in problems:
            _print(f"         - {problem}")
    else:
        _line("OK", "Spotify selectors",
              f"{len(selector_names())} entries validated "
              f"(run 'spotify-check' for the live check)")

    # --- session -------------------------------------------------------
    profile = config.profile_dir()
    if profile.exists() and any(profile.iterdir()):
        _line("OK", "Spotify session", f"saved in {profile}")
    else:
        warnings.append("Spotify is not connected yet.")
        _line("WARN", "Spotify session", f"not created yet - {profile}")

    _print("\n" + "=" * 60)
    for warning in warnings:
        _print(f"[NOTE] {warning}")
    if failures:
        _print(f"\n{len(failures)} problem(s) must be fixed before publishing:")
        for failure in failures:
            _print(f"  - {failure}")
        _print("\nOn Windows, run SETUP.bat to fix most of these automatically.")
        return 1
    if warnings:
        _print("\nEverything needed is in place. "
               "Sign in once with 'python -m app --auth', then start the app.")
    else:
        _print("\nEverything is ready.")
    return 0


def _module_present(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


# ------------------------------------------------------------------- auth --

def cmd_auth(args) -> int:
    from .publishers.spotify.publisher import SpotifyPublisher

    try:
        config = load(args)
    except ConfigError as exc:
        _print(str(exc))
        return 2
    bootstrap_logging(config, quiet=args.quiet)
    config.ensure_directories()
    publisher = SpotifyPublisher(config, database=None)

    _print(BANNER)
    _print("\nSigning in to Spotify for Creators")
    _print("=" * 60)
    _print("\nA browser window is about to open on Spotify's real login page.")
    _print("Sign in there. This application never sees or stores your password,")
    _print("and it never asks you to type it anywhere except Spotify's own form.")
    _print("\nIf Spotify asks for a verification code or two-step verification, complete")
    _print("that in the same window. It is required once, before the first episode.\n")

    result = publisher.auth.interactive_login(
        publisher.session,
        timeout_seconds=args.timeout,
        on_wait=lambda message: _print(f"  {message}"),
    )
    _print("\n" + "-" * 60)
    _print(f"Result: {result.status.value}")
    _print(result.message)
    if result.requires_action:
        _print(f"Next:   {result.requires_action}")
    info = publisher.auth.session_info()
    _print(f"\nSession stored in: {info['profile_dir']}")
    _print(f"Size:              {info['profile_size_bytes'] // 1024} KB")
    publisher.close()
    if result.status.value == "CONNECTED":
        _print("\nYou are signed in. Start the automation with START.bat (or 'python -m app').")
        return 0
    if result.status.value == "NEEDS_VERIFICATION":
        _print("\nSpotify still needs your email address confirmed before publishing.")
        _print("Use the link Spotify emailed you, then run this command again.")
    return 1


def cmd_disconnect(args) -> int:
    from .publishers.spotify.auth import SpotifyAuthenticator

    config = load(args)
    authenticator = SpotifyAuthenticator(config, lambda: None)
    result = authenticator.disconnect()
    _print(result["message"])
    _print(f"Folder checked: {result['profile_removed']}")
    return 0


# ------------------------------------------------------------------- run --

def cmd_run(args) -> int:
    try:
        config = load(args)
    except ConfigError as exc:
        _print(f"\n{exc}\n")
        return 2
    config.ensure_directories()
    bootstrap_logging(config, quiet=args.quiet)

    service = AutomationService(config, ensure_dirs=False)
    service.start()

    dashboard = None
    if config.api.enable and not args.no_dashboard:
        from .api.server import DashboardServer

        dashboard = DashboardServer(
            service, host=config.api.host, port=config.api.port,
            open_browser=config.api.open_browser_on_start or args.open_browser,
        )
        try:
            url = dashboard.start()
        except Exception as exc:  # noqa: BLE001
            _print(f"The dashboard could not start: {exc}")
            url = None
    else:
        url = None

    _print(BANNER)
    _print(f"\nPodcastAutomation is running.\n{'=' * 60}")
    _print(f"  Folder:   {config.root_path()}")
    for show in config.shows:
        _print(f"  Incoming: {config.show_incoming(show)}   [{show.name}]")
    if url:
        _print(f"\n  Dashboard: {url}")
        if dashboard and dashboard.token:
            _print(f"  (the dashboard asks for its local token once; it is also in the "
                   f"address printed above)")
    _print(f"  Logs:      {config.logs_dir()}")
    _print(f"  Database:  {config.database_path()}")
    _print("\nDrop a media file and a same-named .txt file into an incoming folder.")
    _print("Press Ctrl+C to stop.\n")

    stop = threading.Event()

    def _handle(signum, frame):  # pragma: no cover - signal path
        _print("\nShutting down...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # pragma: no cover
            pass

    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        if dashboard:
            dashboard.stop()
        service.stop()
        _print("Stopped. Everything is safe - unfinished jobs resume on the next start.")
    return 0


def cmd_dashboard(args) -> int:
    try:
        config = load(args)
    except ConfigError as exc:
        _print(f"\n{exc}\n")
        return 2
    config.ensure_directories()
    bootstrap_logging(config, quiet=args.quiet)
    service = AutomationService(config, ensure_dirs=False)
    service.start()
    from .api.server import run_server

    _print(BANNER)
    _print(f"\nDashboard: http://{config.api.host}:{config.api.port}/\n")
    try:
        run_server(service, host=config.api.host, port=config.api.port,
                   open_browser=args.open_browser)
    finally:
        service.stop()
    return 0


# ---------------------------------------------------------------- status --

def cmd_status(args) -> int:
    config = load(args)
    bootstrap_logging(config, quiet=True)
    service = AutomationService(config, ensure_dirs=False)
    status = service.status()
    if args.json:
        _print(json.dumps(status, indent=2, default=str))
        return 0

    _print(BANNER)
    _print(f"\nFolder:  {config.root_path()}\nDatabase:{config.database_path()}\n")
    spotify = status["spotify"]
    mark = "connected" if spotify.get("ok") else "NOT connected"
    _print(f"Spotify: {mark} - {spotify.get('summary', '')}")
    for detail in spotify.get("details", []):
        _print(f"         {detail}")
    for problem in spotify.get("problems", []):
        _print(f"  !     {problem}")
    _print(f"\nWatcher: {status['watcher']['watching']} folder(s) watched, "
           f"{status['watcher']['pending_files']} file(s) settling")
    for detail in status["watcher"]["details"]:
        _print(f"         {detail['show']}: {detail['incoming']}"
               f"  ({detail['pending']} pending)")
    _print("\nJobs by state:")
    counts = status["counts"]
    if not counts:
        _print("         (none yet)")
    for state, count in sorted(counts.items()):
        _print(f"         {state:24} {count}")
    _print("\nShows:")
    for show in status["shows"]:
        _print(f"         {show['name']}: {show['incoming']}")
        if show["counts"]:
            published = show["counts"].get("ARCHIVED", 0) + show["counts"].get("PUBLISHED", 0)
            failed = show["counts"].get("FAILED", 0)
            _print(f"            {published} published, {failed} failed")
    service.db.close()
    return 0


def cmd_jobs(args) -> int:
    config = load(args)
    service = AutomationService(config, ensure_dirs=False)
    jobs = service.jobs(limit=args.limit, state=args.state, show_id=args.show or "")
    if args.json:
        _print(json.dumps(jobs, indent=2, default=str))
        service.db.close()
        return 0
    if not jobs:
        _print("No jobs yet.")
    for job in jobs:
        flag = "!" if job["last_error"] else " "
        _print(f"{flag} {job['id'][:8]}  {job['state']:22} {job['title'][:46]:46} "
               f"{job['filename']}")
        if job["last_error"]:
            _print(f"    error: {job['last_error'][:160]}")
    service.db.close()
    return 0


def _resolve_job(service, prefix: str) -> Optional[str]:
    exact = service.db.get_job(prefix)
    if exact:
        return prefix
    matches = [j["id"] for j in service.db.list_jobs(limit=500) if j["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        _print(f"No job starts with {prefix!r}.")
    else:
        _print(f"{prefix!r} matches {len(matches)} jobs: " + ", ".join(m[:8] for m in matches))
    return None


def cmd_retry(args) -> int:
    config = load(args)
    service = AutomationService(config, ensure_dirs=False)
    job_id = _resolve_job(service, args.job_id)
    if not job_id:
        service.db.close()
        return 2
    job = service.retry_job(job_id)
    if job is None:
        state = service.db.get_job(job_id)["state"]
        _print(f"Job {job_id[:8]} is {state} and cannot be retried.")
        service.db.close()
        return 1
    _print(f"Job {job_id[:8]} is now {job['state']}.")
    service.db.close()
    return 0


def cmd_cancel(args) -> int:
    config = load(args)
    service = AutomationService(config, ensure_dirs=False)
    job_id = _resolve_job(service, args.job_id)
    if not job_id:
        service.db.close()
        return 2
    service.cancel_job(job_id)
    _print(f"Cancellation requested for {job_id[:8]}.")
    service.db.close()
    return 0


# ---------------------------------------------------------------- doctor --

def cmd_doctor(args) -> int:
    from .publishers.spotify.diagnostics import run_doctor

    config = load(args)
    bootstrap_logging(config, quiet=True)
    report = run_doctor(config, live=args.live)
    if args.json:
        _print(json.dumps(report.as_dict(), indent=2))
    else:
        _print("\n" + report.render())
    return 0 if report.ok else 1


def cmd_spotify_smoke_test(args) -> int:
    """Read-only live check against the user's real Spotify account.

    Deliberately conservative:
      * verifies authentication;
      * verifies the configured show can be found and its episode list read;
      * reports the show(s) and the latest episode it can see;
      * **does not create, upload or publish anything** unless the user also
        passes --live-publish, which is a separate, explicit act.
    """
    from .publishers.base import AuthStatus
    from .publishers.spotify.publisher import SpotifyPublisher

    config = load(args)
    bootstrap_logging(config, quiet=args.quiet)
    _print(BANNER)
    _print("\nSpotify live smoke test")
    _print("=" * 60)
    _print("This test is READ-ONLY by default. It does not upload or publish.\n")

    publisher = SpotifyPublisher(config)
    session = publisher.session
    failures: list[str] = []
    try:
        _print("[1/4] Starting the browser...")
        session.start()
        _print("      OK")

        _print("[2/4] Opening Spotify and checking the session...")
        auth = publisher.auth.ensure_session(session)
        if auth.status is not AuthStatus.CONNECTED:
            failures.append(f"authentication: {auth.message}")
            _print(f"      FAILED - {auth.message}")
            if auth.requires_action:
                _print(f"      -> {auth.requires_action}")
            _print("\nRun 'python -m app --auth' and sign in, then try again.")
            return 2
        _print(f"      OK - signed in ({auth.url})")

        _print("[3/4] Reading the configured show(s)...")
        from .publishers.spotify.reconciliation import Reconciler

        reconciler = Reconciler(config)
        for show in config.enabled_shows():
            episodes = reconciler.list_remote_episodes(
                session=session, show_config=show, limit=10
            )
            if show.spotify_show_id:
                _print(f"      {show.name}: show id {show.spotify_show_id} -> "
                       f"{len(episodes)} episode(s) readable")
            else:
                _print(f"      {show.name}: NO spotify_show_id configured -> "
                       f"{len(episodes)} episode(s) readable")
            for episode in episodes[:3]:
                _print(f"         - {episode.state:10} {episode.title[:56]}")
            if not episodes:
                _print("         (no episodes listed, or the show has none yet)")

        _print("[4/4] Selector probe on the live pages...")
        from .publishers.spotify.selectors import DASHBOARD, LOGIN

        checked = 0
        missing = []
        for name, selector_set in list(LOGIN.items()) + list(DASHBOARD.items()):
            if any(session.exists(c, timeout_ms=700) for c in selector_set.all()):
                checked += 1
            else:
                missing.append(name)
        _print(f"      {checked} of {len(LOGIN) + len(DASHBOARD)} dashboard/login controls found")
        if missing:
            _print(f"      Not on this page (may be fine): {', '.join(missing)}")

        capture = config.diagnostics_dir() / f"smoke-{time.strftime('%Y%m%d-%H%M%S')}.png"
        shot = session.screenshot(capture)
        if shot:
            _print(f"      Screenshot: {shot}")

        if not args.live_publish:
            _print("\n" + "=" * 60)
            _print("RESULT: authentication and read access WORK.")
            _print("No episode was created, uploaded or published.")
            _print("\nTo perform a real (publishing) test, drop a real episode into the")
            _print("incoming folder and let the app publish it, or re-run with:")
            _print("    python -m app --spotify-smoke-test --live-publish")
            return 0

        _print("\n" + "=" * 60)
        _print("LIVE PUBLISH TEST")
        _print("=" * 60)
        _print("This will create a real episode in your real Spotify account.")
        answer = input("Type PUBLISH to continue: ").strip()
        if answer != "PUBLISH":
            _print("Cancelled. Nothing was published.")
            return 1
        return _live_publish(config, publisher)
    except Exception as exc:  # noqa: BLE001
        from .security.redaction import scrub_exception
        failures.append(scrub_exception(exc))
        _print(f"\nFAILED: {scrub_exception(exc)}")
        return 2
    finally:
        publisher.close()
    if failures:  # pragma: no cover
        return 2
    return 0


def _live_publish(config, publisher) -> int:  # pragma: no cover - needs a real account
    """Publish one clearly-named test episode, end to end."""
    from .core.models import EpisodeMetadata, PublishMode
    from .core.models import MediaKind, MediaInfo
    from .publishers.base import ProgressCallback, PublishRequest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from make_test_media import make_mp3

    stamp = time.strftime("%Y-%m-%d %H:%M")
    title = f"AUTOMATION TEST - do not use - {stamp}"
    work = config.data_dir() / "smoke-test"
    work.mkdir(parents=True, exist_ok=True)
    media = make_mp3(work / "spotify_smoke_test.mp3", seconds=5, title=title)
    _print(f"\nPrepared {media.name} ({media.stat().st_size} bytes)")

    meta = EpisodeMetadata(
        title=title,
        description=(
            "Automated end-to-end test created by PodcastAutomation. "
            "Safe to delete from Spotify for Creators."
        ),
        explicit=False,
        publish=PublishMode.NOW,
        timezone=config.enabled_shows()[0].timezone if config.enabled_shows() else "UTC",
        source_path="(generated by the smoke test)",
    )
    request = PublishRequest(
        show_key=config.enabled_shows()[0].resolved_id() if config.enabled_shows() else "",
        show_name=config.enabled_shows()[0].name if config.enabled_shows() else "",
        media_path=media, metadata=meta,
        media_info=MediaInfo(path=str(media), kind=MediaKind.AUDIO, size=media.stat().st_size),
    )
    result = publisher.publish(request, progress=ProgressCallback(
        lambda stage, message, percent: _print(f"      {stage:12} {message}")
    ))
    _print(f"\nResult: {result.status.value}")
    _print(result.message)
    if result.remote_episode:
        _print(f"Episode id: {result.remote_episode.episode_id}")
        _print(f"URL:        {result.remote_episode.url}")
    return 0 if result.succeeded else 1


# ------------------------------------------------------------------ tests --

def cmd_test(args) -> int:
    root = Path(__file__).resolve().parents[1]
    import subprocess

    cmd = [sys.executable, "-m", "pytest"]
    if args.verbose:
        cmd.append("-v")
    if args.coverage:
        cmd += ["--cov=app", "--cov-report=term-missing", "--cov-report=html"]
    if args.keyword:
        cmd += ["-k", args.keyword]
    cmd += args.paths or [str(root / "tests")]
    return subprocess.call(cmd, cwd=str(root))


def cmd_make_test_media(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import make_test_media
    from make_test_media import write_test_episode

    target = Path(args.folder).expanduser()
    # A metadata file is written unless the user asked for media only, because
    # the app's safe default refuses to publish a .txt-less pair.
    metadata = None if args.no_metadata else make_test_media.DEFAULT_METADATA
    try:
        media, meta = write_test_episode(
            target, args.name, media=args.media, seconds=args.seconds, metadata=metadata,
        )
    except ValueError as exc:
        _print(f"That name cannot be used as a file name: {exc}")
        return 2
    _print(f"Wrote {media}")
    if meta:
        _print(f"Wrote {meta}")
    _print(f"\nDrop these into a show's incoming folder, or use them in {target} directly.")
    return 0


def cmd_init_autostart(args) -> int:
    """Create a Windows Scheduled Task that starts the app at logon."""
    import subprocess

    if os.name != "nt":
        _print("Windows auto-start is only available on Windows.")
        _print("On Linux, add a systemd user service; on macOS, a launchd LaunchAgent.")
        return 1
    project = Path(__file__).resolve().parents[1]
    task_name = args.task_name
    command = f'"{sys.executable}" -m app --config "{project / "config" / "config.yaml"}"'
    _print(f"Creating the scheduled task {task_name!r}...")
    _print(f"  {command}")
    args_list = [
        "schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", task_name,
        "/TR", command, "/RL", "LIMITED",
    ]
    proc = subprocess.run(args_list, shell=False, capture_output=True, text=True)  # noqa: S603
    if proc.returncode == 0:
        _print("\nDone. PodcastAutomation will start automatically when you log in.")
        _print(f"Remove it again with:  schtasks /Delete /F /TN \"{task_name}\"")
        return 0
    _print(f"Failed: {proc.stderr.strip() or proc.stdout.strip()}")
    _print("\nYou may need to run this from an Administrator command prompt.")
    return 1


def cmd_remove_autostart(args) -> int:
    import subprocess

    if os.name != "nt":
        return 1
    proc = subprocess.run(["schtasks", "/Delete", "/F", "/TN", args.task_name],
                          shell=False, capture_output=True, text=True)  # noqa: S603
    _print(proc.stdout.strip() or proc.stderr.strip())
    return 0 if proc.returncode == 0 else 1


# ------------------------------------------------------------------- main --

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Automatic podcast publishing to Spotify for Creators.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Normal use:\n"
            "  Drop MyEpisode.mp4 and MyEpisode.txt into a show's incoming folder.\n"
            "  Everything else is automatic.\n"
        ),
    )
    # The same global options are accepted before *and* after the subcommand,
    # because both "app --root X check" and "app check --root X" are natural
    # things to type, and a confusing "unrecognized arguments" error over
    # argument *order* is a bad first impression.
    def add_globals(target: argparse.ArgumentParser, *, suppress: bool = False) -> None:
        # Subparsers must not carry defaults for the shared options: argparse
        # applies a subparser's defaults *after* the main parser has parsed, so
        # "app --root X status" would end up with root=None. SUPPRESS means
        # "leave the attribute alone unless the user actually passed it".
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        target.add_argument("--config", help="path to config.yaml", default=d(None))
        target.add_argument("--root", help="override the runtime root folder", default=d(None))
        target.add_argument("--port", type=int, help="dashboard port", default=d(None))
        target.add_argument("--headless", action="store_true",
                            help="run the browser without a window (not for first sign-in)",
                            default=d(False))
        target.add_argument("--quiet", action="store_true", help="less console output",
                            default=d(False))
        target.add_argument("--json", action="store_true", help="machine-readable output",
                            default=d(False))

    add_globals(parser)

    sub = parser.add_subparsers(dest="command")

    def new(name: str, *, aliases=(), **kwargs) -> argparse.ArgumentParser:
        child = sub.add_parser(name, aliases=list(aliases), **kwargs)
        add_globals(child, suppress=True)
        return child

    p = new("run", help="run the automation (default)")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--open-browser", action="store_true")
    p.set_defaults(func=cmd_run)

    p = new("setup", help="first-time setup wizard")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_setup)

    p = new("check", help="check the installation")
    p.set_defaults(func=cmd_check)

    p = new("auth", help="sign in to Spotify once")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait")
    p.set_defaults(func=cmd_auth)

    p = new("disconnect", help="delete the saved Spotify session")
    p.set_defaults(func=cmd_disconnect)

    p = new("status", help="print a status summary")
    p.set_defaults(func=cmd_status)

    p = new("jobs", help="list jobs")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--state", default="")
    p.add_argument("--show", default="")
    p.set_defaults(func=cmd_jobs)

    p = new("retry", help="retry a job")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_retry)

    p = new("cancel", help="cancel a job")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_cancel)

    p = new("dashboard", help="run only the dashboard")
    p.add_argument("--open-browser", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    p = new("spotify-check", aliases=["doctor"],
                       help="safe, read-only Spotify connectivity check")
    p.add_argument("--live", action="store_true", help="also open Spotify (read-only)")
    p.set_defaults(func=cmd_doctor)

    p = new("spotify-smoke-test", help="controlled live Spotify check")
    p.add_argument("--live-publish", action="store_true",
                   help="actually publish a clearly-named test episode")
    p.set_defaults(func=cmd_spotify_smoke_test)

    p = new("test", help="run the test suite")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--coverage", action="store_true")
    p.add_argument("-k", dest="keyword", default="")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_test)

    p = new("make-test-media", help="write sample media + metadata files")
    p.add_argument("folder")
    p.add_argument("--name", default="TEST_EPISODE")
    p.add_argument("--media", default="mp4", choices=["mp4", "mov", "mp3", "wav", "mpg"])
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--no-metadata", action="store_true")
    p.set_defaults(func=cmd_make_test_media)

    p = new("install-autostart", help="start automatically on Windows logon")
    p.add_argument("--task-name", default="PodcastAutomation")
    p.set_defaults(func=cmd_init_autostart)

    p = new("remove-autostart", help="stop starting automatically")
    p.add_argument("--task-name", default="PodcastAutomation")
    p.set_defaults(func=cmd_remove_autostart)

    return parser


#: ``python -m app --check`` is the form used in the documentation and in the
#: Windows scripts.  These map it onto the equivalent subcommand so both
#: spellings work and neither surprises a user.
FLAG_ALIASES = {
    "--check": "check",
    "--setup": "setup",
    "--auth": "auth",
    "--disconnect": "disconnect",
    "--status": "status",
    "--jobs": "jobs",
    "--dashboard": "dashboard",
    "--doctor": "spotify-check",
    "--spotify-check": "spotify-check",
    "--spotify-smoke-test": "spotify-smoke-test",
    "--test": "test",
    "--make-test-media": "make-test-media",
    "--install-autostart": "install-autostart",
    "--remove-autostart": "remove-autostart",
}


def _expand_flag_aliases(argv: list[str]) -> list[str]:
    """Turn ``--check`` into ``check``, leaving the rest of the line alone."""
    out: list[str] = []
    for item in argv:
        name = item.split("=", 1)[0]
        if name in FLAG_ALIASES:
            replacement = FLAG_ALIASES[name]
            if "=" in item:
                # ``--make-test-media=foo`` - keep the value with the subcommand.
                out.extend([replacement, item.split("=", 1)[1]])
            else:
                out.append(replacement)
        else:
            out.append(item)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_expand_flag_aliases(raw))
    if not getattr(args, "func", None):
        args = parser.parse_args(_expand_flag_aliases(raw) + ["run"])
    try:
        return args.func(args)
    except ConfigError as exc:
        _eprint(f"\nConfiguration problem:\n{exc}\n")
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        _eprint("\nInterrupted.")
        return 130
