"""Browser session abstraction.

The Spotify publishing flow is written against :class:`BrowserSession`, not
against Playwright directly.  That gives us three things:

1. **Testability without a browser** - the whole flow (auth check, show
   selection, duplicate reconciliation, field filling, publish confirmation) is
   exercised against a real HTTP mock of Spotify's UI.
2. **One place that knows about Playwright** - timeouts, waiting strategies and
   error translation live here.
3. **Honest diagnostics** - when a selector stops matching we capture the page
   HTML, a screenshot, the URL and every selector that was tried, and raise
   :class:`SelectorChanged` with all of it attached.

Waiting strategy
----------------
No arbitrary ``time.sleep()`` in the flow.  Everything waits for a *condition*
(``locator.wait_for``, ``wait_for_url``, ``wait_for_function``) with an explicit
timeout, and a failed wait always produces a diagnostic capture.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Sequence

from ...security.redaction import redact_html, redact_text
from .selectors import SelectorSet

log = logging.getLogger(__name__)

__all__ = [
    "BrowserSession",
    "PlaywrightSession",
    "SessionOptions",
    "SessionError",
    "LoginRequired",
    "SelectorNotFound",
]


class SessionError(RuntimeError):
    """A browser-level failure.  Never contains credentials."""


class LoginRequired(SessionError):
    pass


class SelectorNotFound(SessionError):
    def __init__(self, selector_set: SelectorSet, url: str = "", tried: Sequence[str] = ()):
        self.selector_set = selector_set
        self.url = url
        self.tried = list(tried)
        super().__init__(
            f"Could not find {selector_set.description!r} on {url or 'the current page'}. "
            f"Tried: {', '.join(self.tried[:4])}"
        )


class SessionOptions:
    def __init__(
        self,
        *,
        headless: bool = False,
        slow_mo_ms: int = 0,
        browser: str = "chromium",
        default_timeout_ms: int = 45_000,
        navigation_timeout_ms: int = 120_000,
        locale: str = "de-DE",
        viewport: tuple[int, int] = (1280, 900),
        profile_dir: Optional[Path] = None,
        capture_diagnostics: bool = True,
        diagnostics_dir: Optional[Path] = None,
        base_url: str = "https://creators.spotify.com",
    ):
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.browser = browser
        self.default_timeout_ms = default_timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.locale = locale
        self.viewport = viewport
        self.profile_dir = profile_dir
        self.capture_diagnostics = capture_diagnostics
        self.diagnostics_dir = diagnostics_dir
        self.base_url = base_url.rstrip("/")


class BrowserSession(ABC):
    """The minimum surface the Spotify flow needs."""

    # -- lifecycle -------------------------------------------------------
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def is_open(self) -> bool: ...

    # -- navigation ------------------------------------------------------
    @abstractmethod
    def goto(self, url: str, *, timeout_ms: int | None = None) -> str: ...

    @abstractmethod
    def url(self) -> str: ...

    @abstractmethod
    def wait_for_url(self, pattern: str, *, timeout_ms: int | None = None) -> bool: ...

    # -- queries ---------------------------------------------------------
    @abstractmethod
    def exists(self, selector: str, *, timeout_ms: int = 1500) -> bool: ...

    @abstractmethod
    def wait_for(self, selector: str, *, timeout_ms: int | None = None,
                 state: str = "visible") -> bool: ...

    @abstractmethod
    def count(self, selector: str) -> int: ...

    @abstractmethod
    def text(self, selector: str, *, timeout_ms: int = 2000) -> str: ...

    @abstractmethod
    def texts(self, selector: str) -> list[str]: ...

    @abstractmethod
    def attr(self, selector: str, name: str) -> Optional[str]: ...

    @abstractmethod
    def attrs(self, selector: str, name: str) -> list[Optional[str]]: ...

    @abstractmethod
    def value(self, selector: str, *, timeout_ms: int = 2000) -> str: ...

    # -- actions ---------------------------------------------------------
    @abstractmethod
    def click(self, selector: str, *, timeout_ms: int | None = None) -> None: ...

    @abstractmethod
    def fill(self, selector: str, text: str, *, timeout_ms: int | None = None) -> None: ...

    @abstractmethod
    def press(self, selector: str, key: str) -> None: ...

    @abstractmethod
    def check(self, selector: str, *, timeout_ms: int | None = None) -> None: ...

    @abstractmethod
    def set_input_files(self, selector: str, paths: Sequence[str],
                        *, timeout_ms: int | None = None) -> None: ...

    # -- diagnostics -----------------------------------------------------
    @abstractmethod
    def content(self) -> str: ...

    @abstractmethod
    def screenshot(self, path: Path) -> Optional[Path]: ...

    @abstractmethod
    def evaluate(self, script: str) -> Any: ...

    # -- high level helpers ----------------------------------------------
    def resolve(self, selector_set: SelectorSet, *, timeout_ms: int | None = None,
                required: bool = True) -> Optional[str]:
        """Return the first candidate that matches.  ``None`` if none do.

        This is the "centralised selectors with graceful degradation" behaviour:
        most of the time the primary hook works, and if Spotify renames a
        ``data-testid`` we still find the control by role/label/placeholder.
        """
        for candidate in selector_set.all():
            try:
                if self.wait_for(candidate, timeout_ms=timeout_ms, state=selector_set.state):
                    return candidate
            except Exception as exc:  # noqa: BLE001 - a bad candidate is not fatal
                log.debug("Selector candidate %r failed: %s", candidate, redact_text(str(exc)))
        if required and not selector_set.optional:
            raise SelectorNotFound(selector_set, self.url(), selector_set.all())
        return None

    def any_visible(self, selector_sets: Sequence[SelectorSet], *, timeout_ms: int = 1500) -> Optional[str]:
        for selector_set in selector_sets:
            for candidate in selector_set.all():
                if self.exists(candidate, timeout_ms=timeout_ms):
                    return candidate
        return None

    def capture(self, name: str, *, reason: str = "") -> dict[str, Any]:
        """Save HTML + screenshot for post-mortem.  Returns what was captured."""
        return {"name": name, "reason": redact_text(reason, max_len=500), "url": self.url()}


class PlaywrightSession(BrowserSession):
    """Real Playwright implementation.

    Uses the *sync* API.  The worker runs in its own thread precisely so that
    Playwright's sync API - which forbids an active asyncio event loop in the
    thread - works reliably.
    """

    def __init__(self, options: SessionOptions):
        self.options = options
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._persistent = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise SessionError(
                "Playwright is not installed. Run SETUP.bat, or: "
                "python -m pip install playwright && python -m playwright install chromium"
            ) from exc
        self._pw = sync_playwright().start()
        launcher = self._pw
        engine = self.options.browser
        channel = {"chrome": "chrome", "msedge": "msedge"}.get(engine)
        try:
            if engine == "chromium":
                self._browser = launcher.chromium.launch(
                    headless=self.options.headless, slow_mo=self.options.slow_mo_ms,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            elif engine in {"chrome", "msedge"}:
                self._browser = launcher.chromium.launch(
                    channel=engine, headless=self.options.headless,
                    slow_mo=self.options.slow_mo_ms,
                )
            elif engine == "firefox":
                self._browser = launcher.firefox.launch(
                    headless=self.options.headless, slow_mo=self.options.slow_mo_ms,
                )
            else:  # pragma: no cover - validated by the config model
                raise SessionError(f"Unsupported browser {engine!r}")
        except Exception as exc:
            self._shutdown_playwright()
            hint = ""
            text = redact_text(str(exc))
            if "Executable doesn't exist" in text or "playwright install" in text:
                hint = (" The browser binary is missing - run: python -m playwright install chromium")
            raise SessionError(
                f"Could not start the {engine} browser: {text}{hint}"
            ) from exc

        context_kwargs: dict[str, Any] = {
            "locale": self.options.locale,
            "viewport": {"width": self.options.viewport[0], "height": self.options.viewport[1]},
            "ignore_https_errors": False,
        }
        if self.options.profile_dir:
            # A persistent on-disk profile keeps the legitimate Spotify session
            # between runs, so the user logs in once rather than every time.
            self._launch_persistent(Path(self.options.profile_dir), channel)
        else:
            self._context = self._browser.new_context(**context_kwargs)
        if self._page is None:
            self._context.set_default_timeout(self.options.default_timeout_ms)
            self._context.set_default_navigation_timeout(self.options.navigation_timeout_ms)
            self._page = self._context.new_page()
        self._page.set_default_timeout(self.options.default_timeout_ms)

    def _launch_persistent(self, profile: Path, channel: Optional[str]) -> None:
        """Launch a persistent context rooted at *profile*.

        Only Chromium-family engines support a user-data-dir via the sync API;
        other engines fall back to an ephemeral context, which still works but
        means re-authenticating on each run.
        """
        chromium_family = self.options.browser in {"chromium", "chrome", "msedge"}
        if not chromium_family:
            log.info(
                "Browser %s does not support a persistent profile; using a "
                "temporary session (you will log in again on each run).",
                self.options.browser,
            )
            self._context = self._browser.new_context(
                locale=self.options.locale,
                viewport={"width": self.options.viewport[0], "height": self.options.viewport[1]},
            )
            return
        kwargs: dict[str, Any] = {
            "headless": self.options.headless,
            "slow_mo": self.options.slow_mo_ms,
            "locale": self.options.locale,
            "viewport": {"width": self.options.viewport[0], "height": self.options.viewport[1]},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            kwargs["channel"] = channel
        try:
            profile.mkdir(parents=True, exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(str(profile), **kwargs)
        except (OSError, Exception) as exc:  # noqa: BLE001
            log.warning(
                "Could not use the persistent browser profile (%s); using a "
                "temporary session instead.", redact_text(str(exc))
            )
            self._context = self._browser.new_context(
                locale=self.options.locale,
                viewport={"width": self.options.viewport[0], "height": self.options.viewport[1]},
            )
            return
        self._persistent = True
        self._page = self._context.pages[0] if self._context.pages else None

    def _shutdown_playwright(self) -> None:
        for obj, method in (
            (self._context, "close"), (self._browser, "close"),
        ):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001  # pragma: no cover
            pass
        self._pw = self._browser = self._context = self._page = None

    def close(self) -> None:
        self._shutdown_playwright()
        self._persistent = False

    def is_open(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    @property
    def uses_persistent_profile(self) -> bool:
        return self._persistent

    # -- navigation ------------------------------------------------------
    def goto(self, url: str, *, timeout_ms: int | None = None) -> str:
        self._require_page()
        target = url if url.startswith("http") else f"{self.options.base_url}{url}"
        try:
            self._page.goto(target, timeout=timeout_ms or self.options.navigation_timeout_ms,
                            wait_until="domcontentloaded")
        except Exception as exc:
            raise SessionError(f"Could not open {target}: {redact_text(str(exc))}") from exc
        return self._page.url

    def url(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.url
        except Exception:  # noqa: BLE001  # pragma: no cover
            return ""

    def wait_for_url(self, pattern: str, *, timeout_ms: int | None = None) -> bool:
        self._require_page()
        try:
            self._page.wait_for_url(pattern, timeout=timeout_ms or self.options.default_timeout_ms)
            return True
        except Exception:
            return False

    # -- queries ---------------------------------------------------------
    def exists(self, selector: str, *, timeout_ms: int = 1500) -> bool:
        self._require_page()
        try:
            loc = self._page.locator(selector).first
            loc.wait_for(state="attached", timeout=timeout_ms)
            return loc.count() > 0
        except Exception:
            return False

    def wait_for(self, selector: str, *, timeout_ms: int | None = None,
                 state: str = "visible") -> bool:
        self._require_page()
        try:
            self._page.locator(selector).first.wait_for(
                state=state, timeout=timeout_ms or self.options.default_timeout_ms
            )
            return True
        except Exception:
            return False

    def count(self, selector: str) -> int:
        self._require_page()
        try:
            return self._page.locator(selector).count()
        except Exception:
            return 0

    def text(self, selector: str, *, timeout_ms: int = 2000) -> str:
        self._require_page()
        loc = self._page.locator(selector).first
        try:
            loc.wait_for(state="attached", timeout=timeout_ms)
        except Exception:
            return ""
        try:
            return (loc.inner_text() or "").strip()
        except Exception:
            try:
                return (loc.text_content() or "").strip()
            except Exception:  # noqa: BLE001  # pragma: no cover
                return ""

    def texts(self, selector: str) -> list[str]:
        self._require_page()
        try:
            loc = self._page.locator(selector)
            count = loc.count()
            out = []
            for i in range(min(count, 500)):
                try:
                    out.append((loc.nth(i).inner_text() or "").strip())
                except Exception:  # noqa: BLE001
                    continue
            return out
        except Exception:
            return []

    def attr(self, selector: str, name: str) -> Optional[str]:
        values = self.attrs(selector, name)
        return values[0] if values else None

    def attrs(self, selector: str, name: str) -> list[Optional[str]]:
        self._require_page()
        try:
            loc = self._page.locator(selector)
            count = loc.count()
            out: list[Optional[str]] = []
            for i in range(min(count, 500)):
                try:
                    out.append(loc.nth(i).get_attribute(name))
                except Exception:  # noqa: BLE001
                    out.append(None)
            return out
        except Exception:
            return []

    def value(self, selector: str, *, timeout_ms: int = 2000) -> str:
        self._require_page()
        loc = self._page.locator(selector).first
        try:
            loc.wait_for(state="attached", timeout=timeout_ms)
            return loc.input_value() or ""
        except Exception:
            try:
                return loc.get_attribute("value") or ""
            except Exception:  # noqa: BLE001  # pragma: no cover
                return ""

    # -- actions ---------------------------------------------------------
    def click(self, selector: str, *, timeout_ms: int | None = None) -> None:
        self._require_page()
        timeout = timeout_ms or self.options.default_timeout_ms
        loc = self._page.locator(selector).first
        try:
            loc.scroll_into_view_if_needed(timeout=timeout)
            loc.click(timeout=timeout)
        except Exception as exc:
            # One retry after a normalising click often clears overlay issues.
            try:
                loc.click(timeout=max(2000, timeout // 3), force=True)
            except Exception:
                raise SessionError(
                    f"Could not click {selector!r}: {redact_text(str(exc))}"
                ) from exc

    def fill(self, selector: str, text: str, *, timeout_ms: int | None = None) -> None:
        """Set a field's value.

        Handles the three cases Spotify's UI can present: a plain input, a
        textarea, and a rich-text ``contenteditable``.  For the rich editor we
        try ``fill`` first (Playwright supports contenteditable) and fall back
        to select-all + ``insertText``, which is what a human's keyboard does.
        """
        self._require_page()
        timeout = timeout_ms or self.options.default_timeout_ms
        loc = self._page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
        except Exception as exc:
            raise SelectorNotFound_for(selector, self.url(), str(exc)) from exc
        tag = ""
        try:
            tag = (loc.evaluate("e => e.tagName || ''") or "").lower()
        except Exception:  # noqa: BLE001
            pass
        editable = False
        try:
            editable = bool(loc.evaluate("e => e.isContentEditable === true"))
        except Exception:  # noqa: BLE001
            pass

        if tag in {"input", "textarea"} and not editable:
            self._fill_plain(loc, text, timeout, selector)
            return

        # contenteditable
        try:
            loc.fill(text, timeout=timeout)
            if self._read_back(loc) == text:
                return
        except Exception as exc:
            log.debug("contenteditable fill failed for %s: %s", selector, redact_text(str(exc)))
        try:
            loc.click(timeout=timeout)
            self._page.keyboard.press("ControlOrMeta+a")
            self._page.keyboard.press("Delete")
            self._page.keyboard.insert_text(text)
            if self._read_back(loc) == text:
                return
        except Exception as exc:
            raise SessionError(
                f"Could not type into {selector!r}: {redact_text(str(exc))}"
            ) from exc
        actual = self._read_back(loc)
        raise SessionError(
            f"The field {selector!r} did not keep the value that was typed "
            f"(expected {len(text)} characters, found {len(actual)}). "
            f"The editor may be a custom component that needs a selector update."
        )

    def _fill_plain(self, loc, text: str, timeout: int, selector: str) -> None:
        try:
            loc.fill(text, timeout=timeout)
        except Exception as exc:
            raise SessionError(
                f"Could not type into {selector!r}: {redact_text(str(exc))}"
            ) from exc
        if self._read_back(loc) != text:
            raise SessionError(
                f"The field {selector!r} rejected the value that was typed. "
                f"Check that the text is accepted by this field."
            )

    @staticmethod
    def _read_back(loc) -> str:
        try:
            return loc.evaluate(
                "e => ('value' in e ? e.value : e.innerText || e.textContent || '')"
            ) or ""
        except Exception:  # noqa: BLE001
            return ""

    def press(self, selector: str, key: str) -> None:
        self._require_page()
        self._page.locator(selector).first.press(key)

    def check(self, selector: str, *, timeout_ms: int | None = None) -> None:
        self._require_page()
        loc = self._page.locator(selector).first
        try:
            loc.check(timeout=timeout_ms or self.options.default_timeout_ms)
            return
        except Exception:
            pass
        # Custom toggles are often <div role=switch>; fall back to a click and
        # then verify the resulting state.
        try:
            loc.click(timeout=timeout_ms or self.options.default_timeout_ms)
        except Exception as exc:
            raise SessionError(
                f"Could not toggle {selector!r}: {redact_text(str(exc))}"
            ) from exc
        state = (self.attr(selector, "aria-checked") or "").lower()
        if state in {"false", "0"}:
            try:
                loc.click(timeout=timeout_ms or self.options.default_timeout_ms)
            except Exception:  # noqa: BLE001
                pass

    def set_input_files(self, selector: str, paths: Sequence[str],
                        *, timeout_ms: int | None = None) -> None:
        self._require_page()
        for path in paths:
            p = Path(path)
            if not p.is_file():
                raise SessionError(f"The media file to upload does not exist: {p}")
            if p.stat().st_size == 0:
                raise SessionError(f"The media file to upload is empty: {p}")
        try:
            self._page.locator(selector).first.set_input_files(
                [str(p) for p in paths], timeout=timeout_ms or self.options.default_timeout_ms
            )
        except Exception as exc:
            raise SessionError(
                f"Could not attach {Path(paths[0]).name} to the upload control: "
                f"{redact_text(str(exc))}"
            ) from exc

    # -- diagnostics -----------------------------------------------------
    def content(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.content()
        except Exception:  # noqa: BLE001  # pragma: no cover
            return ""

    def screenshot(self, path: Path) -> Optional[Path]:
        if self._page is None:
            return None
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path), full_page=False)
            return path
        except Exception as exc:  # noqa: BLE001
            log.debug("Screenshot failed: %s", redact_text(str(exc)))
            return None

    def evaluate(self, script: str) -> Any:
        self._require_page()
        try:
            return self._page.evaluate(script)
        except Exception:  # noqa: BLE001
            return None

    def capture(self, name: str, *, reason: str = "") -> dict[str, Any]:
        """Persist the current page for post-mortem.

        The page is captured *while signed in*, so it can contain session
        identifiers, cookies and account details.  It is scrubbed before it
        reaches the disk; a diagnostic that leaked a session would be worse
        than no diagnostic at all.
        """
        info = super().capture(name, reason=reason)
        if not self.options.capture_diagnostics or not self.options.diagnostics_dir:
            return info
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = Path(self.options.diagnostics_dir) / f"{stamp}-{_safe_name(name)}"
        try:
            base.parent.mkdir(parents=True, exist_ok=True)
            html = redact_html(self.content()[:4_000_000])
            (base.with_suffix(".html")).write_text(html, encoding="utf-8")
            info["html"] = str(base.with_suffix(".html"))
        except Exception as exc:  # noqa: BLE001
            log.debug("HTML capture failed: %s", redact_text(str(exc)))
        shot = self.screenshot(base.with_suffix(".png"))
        if shot:
            info["screenshot"] = str(shot)
        return info

    def _require_page(self) -> None:
        if self._page is None or self._page.is_closed():
            raise SessionError("The browser session is not open. Start it first.")


def SelectorNotFound_for(selector: str, url: str, detail: str) -> SessionError:
    return SessionError(
        f"The control {selector!r} did not appear on {url or 'the current page'}. {detail}"
    )


def _safe_name(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name)[:60] or "capture"
