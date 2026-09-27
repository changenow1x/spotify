"""``spotify-doctor`` - figure out what is wrong, fast.

Three levels of check, each usable without a Spotify account:

1. **static** - is the selector registry well-formed?  (No browser at all.)
2. **environment** - are Playwright, the browser binary, ffmpeg and the
   config present?
3. **live** - can we open Spotify, are we signed in, can we see the shows?

The live check *never publishes anything*.  It reads only.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ...core.config import AppConfig
from ...media.ffmpeg import FfmpegTools
from ...security.redaction import redact_text
from .selectors import SELECTORS, validate_registry

log = logging.getLogger(__name__)

__all__ = ["DoctorReport", "run_doctor", "CheckResult"]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    critical: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ok": self.ok, "detail": self.detail,
            "hint": self.hint, "critical": self.critical,
        }

    def line(self) -> str:
        mark = "PASS" if self.ok else ("FAIL" if self.critical else "WARN")
        text = f"[{mark}] {self.name}: {self.detail}"
        if not self.ok and self.hint:
            text += f"\n         -> {self.hint}"
        return text


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, hint: str = "", critical: bool = True) -> None:
        self.checks.append(CheckResult(name, ok, detail, hint, critical))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.critical)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "checks": [c.as_dict() for c in self.checks]}

    def render(self) -> str:
        lines = ["PodcastAutomation - Spotify doctor", "=" * 40]
        lines += [c.line() for c in self.checks]
        lines.append("=" * 40)
        if self.ok:
            lines.append("Everything checked out. Publishing should work.")
        else:
            lines.append("Some critical checks failed - see the hints above.")
        return "\n".join(lines)


def run_doctor(
    config: AppConfig,
    *,
    live: bool = False,
    session_factory=None,
) -> DoctorReport:
    report = DoctorReport()

    # 1. static selector registry
    problems = validate_registry()
    report.add(
        "Selector registry",
        not problems,
        f"{len(SELECTORS)} logical fields, {sum(len(s.all()) for s in SELECTORS.values())} candidate selectors."
        if not problems else "; ".join(problems[:4]),
        hint="Fix the entries in app/publishers/spotify/selectors.py",
    )

    # 2. environment
    try:
        import playwright  # noqa: F401
        report.add("Playwright package", True, f"version {getattr(playwright, '__version__', 'installed')}")
    except ImportError:
        report.add(
            "Playwright package", False,
            "not installed",
            hint="python -m pip install playwright",
        )
        return report

    browser_path = _find_browser()
    report.add(
        "Browser binary", bool(browser_path),
        browser_path or "no Chromium/Chrome installation was found",
        hint="python -m playwright install chromium",
    )

    tools = FfmpegTools(config.media.ffprobe_path)
    report.add(
        "ffprobe (optional)",
        tools.available(),
        tools.version() or "not found - the built-in media reader will be used instead",
        hint="Install ffmpeg and put ffprobe on PATH for full pre-flight checks",
        critical=False,
    )

    root = config.root_path()
    report.add("Runtime folder", root.exists() or True, str(root))
    report.add("Database", config.database_path().exists(), str(config.database_path()))

    profile = config.profile_dir()
    report.add(
        "Saved Spotify session", profile.exists(),
        f"{profile} ({'present' if profile.exists() else 'not created yet - sign in once'})",
        hint="python -m app --auth",
        critical=False,
    )

    for show in config.enabled_shows():
        incoming = config.show_incoming(show)
        ok = incoming.is_dir()
        report.add(
            f"Incoming folder: {show.name}", ok, str(incoming),
            hint="Create the folder, or set a different `incoming:` path in config.yaml",
        )

    if not live:
        report.add("Live Spotify check", True, "skipped (pass --live to run it)", critical=False)
        return report

    # 3. live, read-only
    from .publisher import build_session_options
    from .browser import PlaywrightSession, SessionError

    factory = session_factory or (lambda: PlaywrightSession(build_session_options(config)))
    session = factory()
    try:
        session.start()
    except SessionError as exc:
        report.add("Browser start", False, redact_text(str(exc)),
                   hint="python -m playwright install chromium")
        return report
    report.add("Browser start", True, "the browser opened")
    try:
        session.goto(config.spotify.dashboard_url,
                     timeout_ms=config.spotify.navigation_timeout_ms)
        report.add("Reached Spotify", True, session.url())
    except SessionError as exc:
        report.add("Reached Spotify", False, redact_text(str(exc)),
                   hint="Check your internet connection and any firewall.")
        return report

    from .auth import SpotifyAuthenticator
    from ..base import AuthStatus

    auth = SpotifyAuthenticator(config, factory).ensure_session(session)
    report.add(
        "Spotify sign-in",
        auth.status is AuthStatus.CONNECTED,
        auth.message,
        hint=auth.requires_action or "python -m app --auth",
    )

    if auth.status is AuthStatus.CONNECTED:
        from .selectors import DASHBOARD
        found = False
        for candidate in DASHBOARD["episodes_nav"].all():
            if session.exists(candidate, timeout_ms=1500):
                found = True
                break
        report.add(
            "Episode list reachable", found,
            "found the Episodes entry" if found else "could not find the Episodes entry",
            hint="Spotify may have changed its navigation. Update selectors.py and rerun.",
        )
        report.add(
            "Show list", True,
            "; ".join(
                f"{s.name} -> show_id={s.spotify_show_id or '(not set)'}"
                for s in config.enabled_shows()
            ),
            critical=False,
        )
    return report


def _find_browser() -> str:
    """Locate a Chromium-family browser Playwright could drive."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            if path and Path(path).exists():
                return path
    except Exception:  # noqa: BLE001
        pass
    for name in ("chrome", "msedge", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path("/usr/bin/chromium"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ):
        if candidate.exists():
            return str(candidate)
    return ""
