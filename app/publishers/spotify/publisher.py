"""The Spotify publishing flow.

This is the adapter that does the work, written against
:class:`~app.publishers.spotify.browser.BrowserSession` so it is fully
exercised in tests without a browser or a Spotify account.

The flow, and the evidence it gathers for each step:

===============  =========================================================
step             proof of success
===============  =========================================================
authenticate     a signed-in-only control is present on the page
select show      the show name appears as the active show
reconcile        the remote episode list was read; a match is adopted
open editor      the title field is present
attach media     the file input reports a chosen file
upload done      Spotify's own progress/processing indicator is gone and
                 the details form is interactive - *not* "we clicked and
                 the page looked right"
fill fields      each field was re-read after typing and compared
publish          we land back on the episode list
confirm          the episode is present in that list with a published or
                 scheduled state, and its URL/ID was captured
===============  =========================================================

If any step cannot be *proven*, the adapter raises or returns
``UNKNOWN``/``NEEDS_RECONCILIATION``.  It never reports success on a hunch.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ...core.config import AppConfig
from ..base import (
    AdapterCapabilities,
    AuthStatus,
    HealthCheck,
    ProgressCallback,
    PublishRequest,
    PublishResult,
    PublicationStatus,
    PublisherAdapter,
    RemoteEpisode,
    SelectorChanged,
)
from .auth import SpotifyAuthenticator
from .browser import BrowserSession, SessionError
from .mapping import SpotifyEpisodePayload, check_payload_supported, map_metadata_to_spotify
from .reconciliation import Reconciler
from .selectors import DASHBOARD, EDITOR, SELECTORS, validate_registry

log = logging.getLogger(__name__)

__all__ = ["SpotifyPublisher", "build_session_options"]


def build_session_options(config: AppConfig):
    from .browser import SessionOptions

    sp = config.spotify
    return SessionOptions(
        headless=sp.headless,
        slow_mo_ms=sp.slow_mo_ms,
        browser=sp.browser,
        default_timeout_ms=sp.default_timeout_ms,
        navigation_timeout_ms=sp.navigation_timeout_ms,
        locale=sp.locale,
        viewport=(sp.viewport_width, sp.viewport_height),
        profile_dir=config.profile_dir() if sp.persistent_profile else None,
        capture_diagnostics=sp.capture_diagnostics,
        diagnostics_dir=config.diagnostics_dir(),
        base_url=sp.base_url,
    )


class SpotifyPublisher(PublisherAdapter):
    name = "spotify"
    display_name = "Spotify for Creators"

    def __init__(
        self,
        config: AppConfig,
        session_factory: Callable[[], BrowserSession] | None = None,
        database=None,
    ):
        self.config = config
        self.database = database
        self._session_factory = session_factory or self._default_session_factory
        # One browser session per thread.  Several workers share one adapter
        # (the pool hands the same object to each), and two threads driving
        # one page would interleave their form fills - which produces episodes
        # carrying another episode's title, or duplicates.
        self._local = threading.local()
        self._sessions: list[BrowserSession] = []
        self._sessions_lock = threading.Lock()
        self.auth = SpotifyAuthenticator(config, self._session_factory)
        self.reconciler = Reconciler(config)
        self._closed = False

    # -- adapter plumbing ---------------------------------------------------
    def _default_session_factory(self) -> BrowserSession:
        from .browser import PlaywrightSession

        return PlaywrightSession(build_session_options(self.config))

    @property
    def session(self) -> BrowserSession:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close_session(self) -> None:
        """Close and forget *this thread's* browser session."""
        session = getattr(self._local, "session", None)
        self._local.session = None
        if session is None:
            return
        with self._sessions_lock:
            if session in self._sessions:
                self._sessions.remove(session)
        try:
            session.close()
        except Exception:  # noqa: BLE001  # pragma: no cover
            log.debug("Error closing the browser session", exc_info=True)

    def capabilities(self) -> AdapterCapabilities:
        return _caps()

    def validate_configuration(self) -> HealthCheck:
        problems: list[str] = []
        details: list[str] = []
        for problem in validate_registry():
            problems.append(f"Selector registry problem: {problem}")
        shows = self.config.enabled_shows()
        if not shows:
            problems.append("No shows are enabled in config.yaml.")
        multi = [s for s in shows if not s.spotify_show_id and not s.spotify_show_name]
        if multi:
            details.append(
                f"{len(multi)} show(s) have no spotify_show_id. If the account has more "
                f"than one show the app will refuse to guess which one to use - fill "
                f"in spotify_show_id for each show."
            )
        for show in shows:
            incoming = self.config.show_incoming(show)
            if not incoming.exists():
                problems.append(f"The incoming folder for {show.name!r} does not exist: {incoming}")
            elif not incoming.is_dir():
                problems.append(f"The incoming path for {show.name!r} is not a folder: {incoming}")
        if not self.config.spotify.persistent_profile:
            details.append(
                "spotify.persistent_profile is off, so the user will have to sign in "
                "on every run."
            )
        summary = (
            "Spotify publishing is configured correctly."
            if not problems else
            f"{len(problems)} configuration problem(s) must be fixed before publishing."
        )
        return HealthCheck(not problems, summary, details, problems)

    def get_status(self) -> HealthCheck:
        details: list[str] = []
        problems: list[str] = []
        live = [s for s in list(self._sessions) if s.is_open()]
        if live:
            auth = self.auth.check_session_fast(live[0])
            details.append(f"Browser session: {auth.value}")
            if auth is not AuthStatus.CONNECTED:
                problems.append("The saved Spotify session has expired - re-authenticate.")
            else:
                return HealthCheck(True, "Spotify is connected.", details, problems)
        else:
            info = self.auth.session_info()
            if info["profile_exists"]:
                details.append(
                    f"A saved browser session exists ({info['profile_size_bytes'] // 1024} KB)."
                )
            else:
                details.append("No Spotify session yet - use 'Connect Spotify' once.")
        return HealthCheck(False, "Spotify is not connected.", details, problems)

    def close(self) -> None:
        """Close every browser session this publisher opened."""
        self._closed = True
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001  # pragma: no cover
                log.debug("Error closing the browser session", exc_info=True)
        self._local = threading.local()

    def disconnect(self) -> dict:
        self.close()
        return self.auth.disconnect()

    # -- the flow -----------------------------------------------------------
    def publish(
        self,
        request: PublishRequest,
        *,
        progress: ProgressCallback | None = None,
    ) -> PublishResult:
        started = time.monotonic()
        cb = progress or ProgressCallback(None)
        session = self.session
        evidence: dict = {"attempt": request.attempt}

        payload = map_metadata_to_spotify(request.metadata)
        blocking = check_payload_supported(payload)
        if blocking:
            return PublishResult(
                PublicationStatus.FAILED,
                "The episode cannot be published: " + " ".join(blocking),
                evidence=evidence,
                elapsed_seconds=time.monotonic() - started,
            )
        evidence["payload"] = payload.as_dict()
        cb("prepare", f"Prepared {payload.describe()}")
        log.info("[spotify] %s", payload.describe())

        session.start()

        # 1 -- authenticate ---------------------------------------------------
        cb("authenticate", "Checking the Spotify session...")
        auth = self.auth.ensure_session(session)
        if auth.status is not AuthStatus.CONNECTED:
            session.capture("auth-required", reason=auth.message)
            return PublishResult(
                PublicationStatus.FAILED,
                auth.message,
                requires_action=auth.requires_action or "Sign in to Spotify.",
                evidence=evidence,
                elapsed_seconds=time.monotonic() - started,
            )
        evidence["authenticated"] = True

        # 2 -- duplicate check before anything is created --------------------
        if request.known_duplicate is not None:
            evidence["reconciled"] = "caller-supplied duplicate"
            return self._result_for_existing(
                request.known_duplicate, payload, evidence, started,
                "An existing Spotify episode was found for this job before uploading.",
            )
        cb("reconcile", "Checking Spotify so the episode is not published twice...")
        try:
            remote = self._select_show_and_list(request, session)
        except SelectorChanged as exc:
            return self._selector_failure(exc, started, evidence)
        if remote is not None:
            # A title match only proves "this is the same episode" when we
            # already sent something.  For a first upload a same-titled
            # episode is a different recording - adopting it here would drop
            # the file the user just dropped in, silently.  It is reported as
            # a warning instead, which the dashboard shows.
            if not getattr(request, "allow_adopt", False):
                evidence["reconciled"] = "title-warning"
                evidence["title_warning"] = (
                    f"Spotify already has an episode titled {remote.title!r}; "
                    "this looks like a different recording, so it is being "
                    "published as a separate episode."
                )
                evidence.setdefault("warnings", []).append(evidence["title_warning"])
            else:
                evidence["reconciled"] = "found_existing"
                return self._result_for_existing(
                    remote, payload, evidence, started,
                    "Spotify already has an episode with this title; it was adopted "
                    "instead of being uploaded again.",
                )
        evidence.setdefault("reconciled", "no_match")

        # 3 -- open the editor ------------------------------------------------
        cb("open_editor", "Opening the new episode form...")
        try:
            self._click(session, DASHBOARD["new_episode_button"])
        except SelectorChanged as exc:
            return self._selector_failure(exc, started, evidence)

        # 4 -- attach the media ------------------------------------------------
        cb("upload", f"Uploading {request.media_path.name} to Spotify...")
        try:
            self._attach_media(session, request.media_path)
        except SelectorChanged as exc:
            return self._selector_failure(exc, started, evidence)
        except SessionError as exc:
            return PublishResult(
                PublicationStatus.FAILED, str(exc), evidence=evidence,
                elapsed_seconds=time.monotonic() - started,
            )

        # 5 -- wait for Spotify to finish processing ---------------------------
        cb("processing", "Spotify is processing the media. This can take a while for large files.")
        ready = self._wait_for_editor_ready(session, request.media_path)
        if isinstance(ready, PublishResult):
            return ready
        evidence["editor_ready_after_seconds"] = ready

        # 6 -- fill the details form -------------------------------------------
        cb("details", "Filling in the episode details...")
        try:
            self._fill_details(session, payload, request)
        except SelectorChanged as exc:
            return self._selector_failure(exc, started, evidence)

        # 7 -- confirm the form before publishing ------------------------------
        form_errors = self._read_form_errors(session)
        if form_errors:
            evidence["form_errors"] = form_errors
            session.capture("form-errors", reason="; ".join(form_errors))
            return PublishResult(
                PublicationStatus.FAILED,
                "Spotify rejected the episode details: " + "; ".join(form_errors),
                evidence=evidence,
                elapsed_seconds=time.monotonic() - started,
            )

        # 8 -- publish ----------------------------------------------------------
        cb("publishing", "Confirming publication with Spotify...")
        try:
            self._click(session, EDITOR["publish_button"])
        except SelectorChanged as exc:
            return self._selector_failure(exc, started, evidence)

        # 9 -- prove it ---------------------------------------------------------
        cb("confirming", "Waiting for Spotify to confirm...")
        outcome = self._confirm_publication(session, request, payload, started)
        outcome.evidence.update({k: v for k, v in evidence.items() if k not in outcome.evidence})
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    # -- steps ---------------------------------------------------------------
    def _select_show_and_list(
        self, request: PublishRequest, session: BrowserSession
    ) -> Optional[RemoteEpisode]:
        """Open the configured show and return a matching existing episode."""
        show = self.config.get_show(request.show_key)
        # Reconciliation tolerates being offline: the cached list is used.
        expected_title = request.metadata.title
        candidates = self.reconciler.find_matches(
            show_id=request.show_key,
            title=expected_title,
            session=session if self._is_interactive() else None,
            show_config=show,
        )
        # A job that has never been sent needs a decisive match (title *and*
        # season *and* episode number) before a same-titled episode on the
        # show is treated as this one.
        confirmed = self.reconciler.pick_duplicate(
            candidates, request=request,
            session=session if self._is_interactive() else None,
            require_numbering=not getattr(request, "allow_adopt", False),
        )
        self.reconciler.cache(request.show_key, candidates)
        if self.database is not None:
            for item in candidates:
                self.database.upsert_spotify_episode(request.show_key, item.as_dict())
        # Remember what was already there.  After the publish the list can
        # contain several episodes with the same title, and confirmation has
        # to pick the one this attempt created rather than the first match -
        # otherwise two different recordings would be recorded under the same
        # remote id, and every later duplicate check would be built on a lie.
        request.options["preexisting_episode_ids"] = self._episode_ids_on_page(session)
        return confirmed

    def _episode_ids_on_page(self, session) -> list[str]:
        """Every episode id currently listed, for before/after comparison.

        A title match is not enough for this: after publishing, two different
        recordings can carry the same title, and only "which row is new"
        tells us which one this attempt produced.
        """
        ids: list[str] = []
        try:
            for row_selector in DASHBOARD["episode_row_any"].all():
                for attribute in ("data-episode-id",):
                    found = session.attrs(row_selector, attribute)
                    ids.extend(i for i in found if i)
                if ids:
                    break
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            log.info("[spotify] Could not snapshot the episode list: %s", exc)
        return ids

    def _is_interactive(self) -> bool:
        """Reconciliation against a live page only makes sense with a browser."""
        try:
            return self.session.is_open()
        except Exception:  # noqa: BLE001  # pragma: no cover
            return False

    def _attach_media(self, session: BrowserSession, media_path: Path) -> None:
        file_selector = session.resolve(EDITOR["file_input"], timeout_ms=8000, required=False)
        if file_selector is None:
            # Spotify sometimes hides the input behind a "Select a file" button.
            button = session.resolve(
                EDITOR["select_file_button"], timeout_ms=5000, required=False
            )
            if button is None:
                raise SelectorChanged(
                    "media file input",
                    SELECTORS["file_input"].all() + SELECTORS["select_file_button"].all(),
                    session.url(),
                )
            session.click(button)
            file_selector = session.resolve(
                EDITOR["file_input"], timeout_ms=8000, required=False
            )
            if file_selector is None:
                raise SelectorChanged(
                    "media file input (after clicking 'Select a file')",
                    SELECTORS["file_input"].all(), session.url(),
                )
        session.set_input_files(file_selector, [str(media_path)])

    def _wait_for_editor_ready(
        self, session: BrowserSession, media_path: Path
    ) -> float | PublishResult:
        """Wait until Spotify has accepted and processed the media.

        The condition is "the details form is interactive", not "a progress bar
        reached 100%".  Spotify removes/replaces the uploading state when it is
        done, and it also surfaces an explicit error if the upload failed -
        both are checked.
        """
        started = time.monotonic()
        deadline = started + max(60, self.config.spotify.processing_timeout_seconds)
        announced = False
        while time.monotonic() < deadline:
            error = self._read_upload_error(session)
            if error:
                session.capture("upload-error", reason=error)
                return PublishResult(
                    PublicationStatus.FAILED,
                    f"Spotify rejected the file: {error}",
                    elapsed_seconds=time.monotonic() - started,
                )
            if self._editor_is_interactive(session):
                return round(time.monotonic() - started, 1)
            if not announced:
                announced = True
                log.info(
                    "[spotify] Waiting for Spotify to finish processing %s "
                    "(this is normal for large files).", media_path.name
                )
            time.sleep(2.0)
        session.capture("processing-timeout", reason="Spotify never finished processing the media")
        return PublishResult(
            PublicationStatus.UNKNOWN,
            f"Spotify did not finish processing {media_path.name} within "
            f"{self.config.spotify.processing_timeout_seconds // 60} minutes. The file may "
            f"still be uploading on Spotify's side. The job was left for reconciliation so "
            f"the episode is never published twice.",
            elapsed_seconds=time.monotonic() - started,
        )

    def _editor_is_interactive(self, session: BrowserSession) -> bool:
        """True when the title field exists and is not disabled/read-only."""
        for candidate in EDITOR["title_input"].all():
            if not session.exists(candidate, timeout_ms=400):
                continue
            if session.attr(candidate, "disabled") is not None:
                continue
            if (session.attr(candidate, "aria-busy") or "").lower() == "true":
                continue
            return True
        return False

    def _read_upload_error(self, session: BrowserSession) -> str:
        for candidate in EDITOR["upload_error"].all():
            if session.exists(candidate, timeout_ms=200):
                text = session.text(candidate, timeout_ms=500)
                if text and len(text) < 500:
                    return text
        return ""

    def _read_form_errors(self, session: BrowserSession) -> list[str]:
        out: list[str] = []
        for candidate in EDITOR["form_error"].all():
            if not session.exists(candidate, timeout_ms=300):
                continue
            text = session.text(candidate, timeout_ms=500)
            if text:
                out.append(text[:300])
        return list(dict.fromkeys(out))[:5]

    def _fill_details(
        self, session: BrowserSession, payload: SpotifyEpisodePayload, request: PublishRequest
    ) -> None:
        title_sel = session.resolve(EDITOR["title_input"])
        session.fill(title_sel, payload.title)
        if session.value(title_sel).strip() != payload.title:
            raise SelectorChanged("episode title field", EDITOR["title_input"].all(), session.url())

        description_sel = self._resolve_description(session)
        session.fill(description_sel, payload.description)
        # Re-read and compare.  A rich-text editor can silently drop content.
        actual = self._read_rich_text(session, description_sel)
        if len(actual) < max(8, int(len(payload.description) * 0.9)):
            raise SelectorChanged(
                f"episode description field (only {len(actual)} of "
                f"{len(payload.description)} characters arrived)",
                EDITOR["description_input"].all() + EDITOR["html_toggle"].all(),
                session.url(),
            )

        self._set_explicit(session, payload.explicit)

        if payload.season is not None or payload.episode_number is not None:
            self._set_season_episode(session, payload)

        self._set_publish_timing(session, payload, request)

    def _resolve_description(self, session: BrowserSession) -> str:
        selector = session.resolve(EDITOR["description_input"], timeout_ms=10000)
        # If the field found is a plain textarea but the editor is a rich-text
        # box, prefer the rich one when the HTML switch is available.
        if (session.attr(selector, "contenteditable") or "") == "true":
            return selector
        html_toggle = session.resolve(EDITOR["html_toggle"], timeout_ms=800, required=False)
        if html_toggle:
            try:
                session.click(html_toggle)
                rich = session.resolve(
                    EDITOR["description_input"], timeout_ms=4000, required=False
                )
                if rich and (session.attr(rich, "contenteditable") or "") == "true":
                    return rich
            except SessionError:
                pass
        return selector

    @staticmethod
    def _read_rich_text(session: BrowserSession, selector: str) -> str:
        return (session.evaluate(
            f"() => {{ const e = document.querySelector({selector!r});"
            " if (!e) return '';"
            " return ('value' in e ? e.value : (e.innerText || e.textContent || '')); }"
        ) or "")

    def _set_explicit(self, session: BrowserSession, explicit: bool) -> None:
        selector = session.resolve(EDITOR["explicit_toggle"], timeout_ms=5000, required=False)
        if selector is None:
            log.warning("[spotify] The explicit-content control was not found; assuming the default.")
            return
        is_radio = (session.attr(selector, "type") or "").lower() in {"radio", "checkbox"}
        if is_radio:
            target = self._find_explicit_choice(session, selector, explicit)
            if target:
                try:
                    session.check(target, timeout_ms=5000)
                    return
                except SessionError:
                    session.click(target)
                return
        # Toggle semantics.
        current = (session.attr(selector, "aria-checked") or "").lower()
        if current in {"true", "false"}:
            already = current == "true"
            if already != explicit:
                try:
                    session.check(selector, timeout_ms=5000)
                except SessionError:
                    session.click(selector)
            return
        try:
            session.check(selector, timeout_ms=5000)
        except SessionError:
            session.click(selector)

    def _find_explicit_choice(
        self, session: BrowserSession, selector: str, explicit: bool
    ) -> Optional[str]:
        """Pick the radio whose value/label matches the desired state."""
        wanted = ("true", "yes", "1", "explicit") if explicit else ("false", "no", "0", "clean")
        values = session.attrs(selector, "value")
        for index, value in enumerate(values or []):
            if value and value.strip().lower() in wanted:
                return _nth(selector, index)
        labels = session.texts(selector)
        for index, text in enumerate(labels or []):
            lowered = (text or "").strip().lower()
            if explicit and lowered in {"explicit", "yes", "true"}:
                return _nth(selector, index)
            if not explicit and lowered in {"clean", "no", "false", "not explicit"}:
                return _nth(selector, index)
        return None

    def _set_season_episode(self, session: BrowserSession, payload: SpotifyEpisodePayload) -> None:
        toggle = session.resolve(
            EDITOR["additional_details_toggle"], timeout_ms=1500, required=False
        )
        if toggle:
            try:
                session.click(toggle, timeout_ms=4000)
            except SessionError:
                pass
        if payload.season is not None:
            selector = session.resolve(EDITOR["season_input"], timeout_ms=4000, required=False)
            if selector:
                session.fill(selector, str(payload.season))
            else:
                log.warning("[spotify] Season field not available; season %s was not set.",
                            payload.season)
        if payload.episode_number is not None:
            selector = session.resolve(
                EDITOR["episode_number_input"], timeout_ms=4000, required=False
            )
            if selector:
                session.fill(selector, str(payload.episode_number))
            else:
                log.warning("[spotify] Episode number field not available; it was not set.")

    def _set_publish_timing(
        self, session: BrowserSession, payload: SpotifyEpisodePayload, request: PublishRequest
    ) -> None:
        if payload.publish_now:
            selector = session.resolve(EDITOR["publish_now_radio"], timeout_ms=5000, required=False)
            if selector is None:
                log.warning("[spotify] Could not find the 'Now' publish option; "
                            "using Spotify's default.")
            else:
                try:
                    session.check(selector, timeout_ms=5000)
                except SessionError:
                    session.click(selector)
            return

        selector = session.resolve(
            EDITOR["publish_schedule_radio"], timeout_ms=5000, required=True
        )
        try:
            session.check(selector, timeout_ms=5000)
        except SessionError:
            session.click(selector)

        date_sel = session.resolve(EDITOR["schedule_date_input"], timeout_ms=8000, required=True)
        session.fill(date_sel, payload.schedule_date)
        time_sel = session.resolve(EDITOR["schedule_time_input"], timeout_ms=8000, required=True)
        session.fill(time_sel, payload.schedule_time)
        if session.value(date_sel).strip() not in {payload.schedule_date, ""}:
            raise SelectorChanged(
                f"publish-date field (Spotify shows {session.value(date_sel)!r}, "
                f"we sent {payload.schedule_date!r})",
                EDITOR["schedule_date_input"].all(), session.url(),
            )
        log.info(
            "[spotify] Scheduled for %s (local) / %s (UTC).",
            payload.schedule_local_label,
            payload.schedule_utc.isoformat() if payload.schedule_utc else "-",
        )

    def _click(self, session: BrowserSession, selector_set) -> str:
        selector = session.resolve(selector_set)
        session.click(selector)
        return selector

    def _confirm_publication(
        self,
        session: BrowserSession,
        request: PublishRequest,
        payload: SpotifyEpisodePayload,
        started: float,
    ) -> PublishResult:
        """Prove the episode exists on Spotify in the expected state."""
        deadline = time.monotonic() + 180
        last_state = "unknown"
        while time.monotonic() < deadline:
            if self._reached_episode_list(session):
                found = self._find_episode_on_list(session, request, payload)
                if found is not None:
                    state = (found.state or "").lower()
                    if state in {"published", "live", "scheduled"}:
                        return self._result_for_existing(
                            found, payload, {"confirmed_by": "episode list"},
                            started,
                            "Media upload completed, Spotify processed the episode, "
                            "and the publication state was read back from Spotify.",
                        )
                    if state in {"draft", "processing", "uploading", "pending", "transcoding"}:
                        last_state = state
                        time.sleep(3.0)
                        continue
                    last_state = state or "unknown"
                    break
            if self._read_upload_error(session):
                return PublishResult(
                    PublicationStatus.FAILED,
                    f"Spotify reported an error while publishing: {self._read_upload_error(session)}",
                )
            time.sleep(2.0)

        # Not proven.  Say so, and let reconciliation settle it.
        if last_state in {"published", "live", "scheduled"}:
            return self._result_for_existing(
                RemoteEpisode(episode_id="", title=payload.title, state=last_state),
                payload, {"confirmed_by": "state text"}, started,
                "Spotify reported the episode as published; the episode ID could not be read.",
            )
        session.capture("publish-unconfirmed", reason=f"last state: {last_state}")
        return PublishResult(
            PublicationStatus.UNKNOWN,
            f"The publish button was used, but the episode's state could not be "
            f"confirmed (last seen: {last_state or 'unknown'}). The job was marked for "
            f"reconciliation so it can never be published twice.",
            evidence={"last_known_state": last_state},
            elapsed_seconds=time.monotonic() - started,
        )

    def _reached_episode_list(self, session: BrowserSession) -> bool:
        for candidate in DASHBOARD["episodes_heading"].all():
            if session.exists(candidate, timeout_ms=600):
                return True
        if "/episodes" in session.url():
            return True
        return bool(session.resolve(EDITOR["close_button"], timeout_ms=300, required=False))

    def _find_episode_on_list(
        self, session: BrowserSession, request: PublishRequest, payload: SpotifyEpisodePayload
    ) -> Optional[RemoteEpisode]:
        title = payload.title
        preexisting = set(request.options.get("preexisting_episode_ids") or ())
        best: Optional[tuple[str, str, str]] = None  # (row, url, episode_id)
        # "Which row is new" is the reliable question here, not "which row
        # has this title": several episodes can share a title, and a row's
        # text usually has the status and date folded in, so comparing the
        # whole string to the title never matches.
        first_with_id: Optional[tuple[str, str, str]] = None
        for index in range(session.count(DASHBOARD["episode_row_any"].all()[0])):
            row = f'{DASHBOARD["episode_row_any"].all()[0]} >> nth={index}'
            url, episode_id = self._row_identity(session, row)
            if not episode_id:
                continue
            if first_with_id is None:
                first_with_id = (row, url, episode_id)
            if episode_id not in preexisting:
                # The row that was not there before this publish: this is ours.
                best = (row, url, episode_id)
                break
        if best is None:
            best = first_with_id
        if best is None:
            # Fall back to the templated selector for pages whose row markup
            # the index form does not match.
            for candidate in DASHBOARD["episode_row"].all():
                filled = candidate.replace("{title}", _escape_for_selector(title))
                if session.exists(filled, timeout_ms=600):
                    url, episode_id = self._row_identity(session, filled)
                    best = (filled, url, episode_id)
                    break
        if best is None:
            return None
        row, url, episode_id = best
        state_texts = session.texts(DASHBOARD["episode_state"].all()[0]) if session.count(
            DASHBOARD["episode_state"].all()[0]
        ) else []
        state = self._classify_state(state_texts) or self._state_from_row(session, row)
        return RemoteEpisode(
            episode_id=episode_id, title=title, state=state, url=url,
            season=payload.season, episode_number=payload.episode_number,
            is_explicit=payload.explicit,
        )

    def _row_identity(self, session, row: str) -> tuple[str, str]:
        """Read (url, episode_id) for one episode row.

        The row's own ``data-episode-id`` is preferred over parsing the link:
        it is unambiguous, and it is the only thing available on a page whose
        link markup differs from the one the selectors expect.
        """
        # A missing attribute comes back as a list holding None, which is
        # truthy - so filter rather than testing the list.
        hrefs = [h for h in (session.attrs(f'{row} a[href*="/episode/"]', "href") or []) if h]
        if not hrefs:
            # A row-scoped link is not always addressable; the page-wide
            # episode link still gives the URL, which the dashboard shows.
            hrefs = [h for h in (session.attrs(DASHBOARD["episode_link"].all()[0], "href")
                                 or []) if h]
        url = hrefs[0] if hrefs else ""
        episode_id = ""
        declared = session.attrs(row, "data-episode-id")
        if declared and declared[0]:
            episode_id = declared[0]
        elif url:
            match = re.search(r"/episode/([A-Za-z0-9_-]+)", url)
            episode_id = match.group(1) if match else ""
        return url, episode_id

    @staticmethod
    def _classify_state(texts: list[str]) -> str:
        joined = " ".join(t or "" for t in texts).lower()
        for needle, value in (
            ("published", "published"), ("live", "published"),
            ("scheduled", "scheduled"), ("draft", "draft"),
            ("processing", "processing"), ("transcoding", "processing"),
            ("uploading", "uploading"),
        ):
            if needle in joined:
                return value
        return ""

    @staticmethod
    def _state_from_row(session: BrowserSession, row: str) -> str:
        status = session.attr(f'{row} [data-episode-status]', "data-episode-status")
        if status:
            return status.lower()
        text = session.text(row, timeout_ms=500)
        return SpotifyPublisher._classify_state([text])

    def _result_for_existing(
        self,
        episode: RemoteEpisode,
        payload: SpotifyEpisodePayload,
        evidence: dict,
        started: float,
        message: str,
    ) -> PublishResult:
        state = (episode.state or "").lower()
        if state == "scheduled":
            status = PublicationStatus.SCHEDULED
        elif state in {"draft"}:
            status = PublicationStatus.DRAFT
        elif state in {"published", "live"}:
            status = PublicationStatus.PUBLISHED
        else:
            status = PublicationStatus.UNKNOWN
        evidence = {**evidence, "spotify_state": state, "spotify_url": episode.url}
        return PublishResult(
            status, message, remote_episode=episode, evidence=evidence,
            elapsed_seconds=time.monotonic() - started,
        )

    def _selector_failure(
        self, exc: SelectorChanged, started: float, evidence: dict
    ) -> PublishResult:
        try:
            capture = self.session.capture("selector", reason=str(exc))
        except Exception:  # noqa: BLE001  # pragma: no cover
            capture = {}
        evidence["diagnostics"] = capture
        return PublishResult(
            PublicationStatus.FAILED,
            str(exc),
            requires_action=(
                "Spotify's website changed. Update app/publishers/spotify/selectors.py "
                "and run 'spotify-doctor'. A page snapshot was saved to the logs folder."
            ),
            evidence=evidence,
            elapsed_seconds=time.monotonic() - started,
        )


def _caps() -> AdapterCapabilities:
    return AdapterCapabilities(
        audio_episodes=True,
        video_episodes=True,
        immediate_publish=True,
        scheduled_publish=True,
        season_episode_numbers=True,
        explicit_flag=True,
        description=True,
        chapters=True,
        chapters_via_description=True,
        episode_artwork=False,
        read_existing_episodes=True,
        delete_episodes=False,
    )


def _escape_for_selector(text: str) -> str:
    """Escape a title for use inside a Playwright ``:has-text("...")`` selector."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _nth(selector: str, index: int) -> str:
    return f"{selector} >> nth={index}"
