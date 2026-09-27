"""Spotify for Creators authentication.

Design, in order of importance:

1. **The human logs in.**  A visible browser window opens on Spotify's real
   login page and the user types their own password into Spotify's own form.
   The application never sees, asks for, stores or logs a password.
2. **Any second factor is the human's.**  Two-step verification, email codes
   and the "confirm your email" step are all completed by the user in that same
   window.  Nothing here bypasses a security control.
3. **The session is kept locally.**  Chromium runs with a persistent
   user-data-dir under ``<root>/data/browser-profile``.  Cookies and tokens
   live there, in the browser's own encrypted store - not in our config, our
   database, or our logs.  On the next run the same profile is reopened and
   the user stays signed in.
4. **Disconnection is explicit.**  ``disconnect()`` deletes the profile
   directory, which is the honest way to "log out" of a browser session.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from typing import Callable

from ...core.config import AppConfig
from ...security.redaction import redact_text
from ..base import AuthStatus
from .browser import BrowserSession, SessionError
from .selectors import LOGIN

log = logging.getLogger(__name__)

__all__ = ["AuthResult", "SpotifyAuthenticator", "AUTH_SETTINGS_KEY"]

AUTH_SETTINGS_KEY = "spotify.auth"


@dataclass
class AuthResult:
    status: AuthStatus
    message: str
    requires_action: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {AuthStatus.CONNECTED, AuthStatus.NEEDS_VERIFICATION}

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "message": self.message,
            "requires_action": self.requires_action,
            "url": self.url,
        }


class SpotifyAuthenticator:
    def __init__(self, config: AppConfig, session_factory: Callable[[], BrowserSession]):
        self.config = config
        self.session_factory = session_factory
        self.last_status: AuthStatus = AuthStatus.UNKNOWN
        self.last_message = ""

    # -- status -----------------------------------------------------------
    def probe(self, session: BrowserSession, *, timeout_ms: int = 8000) -> AuthResult:
        """Classify the current page without ever failing loudly."""
        url = session.url()
        try:
            if self._matches(session, LOGIN["logged_in_marker"], timeout_ms=1200):
                self.last_status = AuthStatus.CONNECTED
                return AuthResult(
                    AuthStatus.CONNECTED, "Spotify session is active.", url=url
                )
            if self._matches(session, LOGIN["verify_email_marker"], timeout_ms=1200):
                self.last_status = AuthStatus.NEEDS_VERIFICATION
                return AuthResult(
                    AuthStatus.NEEDS_VERIFICATION,
                    "Spotify wants you to confirm your email address. This is "
                    "required once before the first episode can be published.",
                    requires_action="Confirm your email address using the link Spotify sent you.",
                    url=url,
                )
            if self._matches(session, LOGIN["two_step_marker"], timeout_ms=1200):
                self.last_status = AuthStatus.NEEDS_HUMAN
                return AuthResult(
                    AuthStatus.NEEDS_HUMAN,
                    "Spotify is asking for two-step verification. Complete it in "
                    "the open browser window.",
                    requires_action="Enter your two-step code in the Spotify window.",
                    url=url,
                )
            if self._matches(session, LOGIN["login_marker"], timeout_ms=1200):
                self.last_status = AuthStatus.NOT_CONNECTED
                return AuthResult(
                    AuthStatus.NOT_CONNECTED,
                    "Not signed in to Spotify yet.",
                    requires_action="Run 'Connect Spotify' and sign in once.",
                    url=url,
                )
            if "accounts.spotify.com" in url or "/pod/login" in url:
                self.last_status = AuthStatus.NOT_CONNECTED
                return AuthResult(
                    AuthStatus.NOT_CONNECTED, "Spotify requires signing in again.",
                    requires_action="Sign in to Spotify in the open browser window.",
                    url=url,
                )
            if url:
                self.last_status = AuthStatus.CONNECTED
                return AuthResult(AuthStatus.CONNECTED, "Spotify session looks active.", url=url)
        except Exception as exc:  # noqa: BLE001
            log.debug("Auth probe failed: %s", redact_text(str(exc)))
        return AuthResult(AuthStatus.UNKNOWN, "Could not determine the Spotify session state.", url=url)

    @staticmethod
    def _matches(session: BrowserSession, selector_set, *, timeout_ms: int) -> bool:
        for candidate in selector_set.all():
            if session.exists(candidate, timeout_ms=min(timeout_ms, 800)):
                return True
        return False

    # -- interactive login --------------------------------------------------
    def interactive_login(
        self,
        session: BrowserSession,
        *,
        timeout_seconds: int = 600,
        on_wait: Callable[[str], None] | None = None,
    ) -> AuthResult:
        """Open Spotify's real login page and wait for the human to finish.

        The user may need several minutes (password manager, email code, 2FA).
        We poll for a signed-in state and give up only after
        *timeout_seconds*.
        """
        session.start()
        login_url = self.config.spotify.login_url
        try:
            session.goto(login_url, timeout_ms=self.config.spotify.navigation_timeout_ms)
        except SessionError as exc:
            return AuthResult(AuthStatus.NOT_CONNECTED, f"Could not open Spotify: {exc}")

        if on_wait:
            on_wait(
                "A Spotify window has opened. Sign in there - this app never sees "
                "your password. If Spotify asks for a verification code, enter it "
                "in that window too."
            )

        deadline = time.monotonic() + max(30, timeout_seconds)
        last_seen = ""
        while time.monotonic() < deadline:
            result = self.probe(session, timeout_ms=2500)
            if result.status is AuthStatus.CONNECTED:
                self.last_status = AuthStatus.CONNECTED
                if on_wait:
                    on_wait("Signed in. Finishing up...")
                return AuthResult(AuthStatus.CONNECTED, "Spotify connected.", url=result.url)
            if result.status is AuthStatus.NEEDS_VERIFICATION:
                # Confirmation is an account-level prerequisite for publishing;
                # report it rather than waiting the user out silently.
                self.last_status = AuthStatus.NEEDS_VERIFICATION
                return AuthResult(
                    AuthStatus.NEEDS_VERIFICATION,
                    "Signed in, but Spotify still needs your email address "
                    "confirmed before episodes can be published.",
                    requires_action=result.requires_action,
                    url=result.url,
                )
            if result.message != last_seen and on_wait:
                last_seen = result.message
                on_wait("Waiting for you to finish signing in to Spotify...")
            time.sleep(2)

        self.last_status = AuthStatus.NEEDS_HUMAN
        return AuthResult(
            AuthStatus.NEEDS_HUMAN,
            f"Spotify sign-in was not completed within {timeout_seconds // 60} minutes. "
            f"Nothing has been published. Run 'Connect Spotify' again when you are ready.",
            requires_action="Sign in to Spotify in the browser window.",
        )

    def ensure_session(
        self,
        session: BrowserSession,
        *,
        start_session: bool = True,
    ) -> AuthResult:
        """Reuse the persisted profile; only open a login window if needed."""
        if start_session:
            session.start()
        target = self.config.spotify.dashboard_url
        try:
            session.goto(target, timeout_ms=self.config.spotify.navigation_timeout_ms)
        except SessionError as exc:
            return AuthResult(AuthStatus.NOT_CONNECTED, f"Could not reach Spotify: {exc}")
        result = self.probe(session)
        self.last_status = result.status
        if result.status is AuthStatus.NOT_CONNECTED:
            # One more chance: the dashboard may redirect late.
            time.sleep(1.0)
            result = self.probe(session, timeout_ms=3000)
            self.last_status = result.status
        return result

    def check_session_fast(self, session: BrowserSession) -> AuthStatus:
        """Cheap check for the dashboard: does the persisted profile still work?"""
        if not session.is_open():
            return AuthStatus.UNKNOWN
        try:
            if not self._matches(session, LOGIN["logged_in_marker"], timeout_ms=500):
                return AuthStatus.EXPIRED if session.url() else AuthStatus.UNKNOWN
            return AuthStatus.CONNECTED
        except Exception:  # noqa: BLE001
            return AuthStatus.UNKNOWN

    # -- disconnect ---------------------------------------------------------
    def disconnect(self) -> dict:
        """Remove the stored browser session.  Spotify is signed out."""
        profile = self.config.profile_dir()
        removed = False
        freed = 0
        if profile.exists():
            try:
                freed = sum(f.stat().st_size for f in profile.rglob("*") if f.is_file())
                shutil.rmtree(profile, ignore_errors=True)
                removed = not profile.exists()
            except OSError as exc:  # pragma: no cover
                log.warning("Could not remove the browser profile: %s", redact_text(str(exc)))
        self.last_status = AuthStatus.NOT_CONNECTED
        return {
            "disconnected": removed,
            "profile_removed": str(profile),
            "bytes_freed": freed,
            "message": (
                "The saved Spotify session was deleted. You will be asked to sign "
                "in again the next time an episode is published."
                if removed else
                "No stored Spotify session was found on this computer."
            ),
        }

    def session_info(self) -> dict:
        profile = self.config.profile_dir()
        exists = profile.exists()
        size = 0
        if exists:
            try:
                size = sum(f.stat().st_size for f in profile.rglob("*") if f.is_file())
            except OSError:  # pragma: no cover
                size = 0
        return {
            "profile_dir": str(profile),
            "profile_exists": exists,
            "profile_size_bytes": size,
            "last_status": self.last_status.value,
            "login_url": self.config.spotify.login_url,
        }
