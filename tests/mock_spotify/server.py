"""A local mock of the Spotify for Creators web interface.

This exists so the **entire** publishing flow can be tested for real, offline,
without a Spotify account: an HTTP server that serves pages shaped like S4C's
(``ftyp``-free HTML, but with the same roles, labels, ``data-testid`` hooks and
state vocabulary the real site uses), accepts a file upload, runs a simulated
"Spotify processing" delay, stores the episode, and exposes an episode list
with published/scheduled/draft states.

It is a **test fixture**, not part of the shipped application.  Pointing the
real publisher at it is only possible by overriding ``spotify.base_url`` in a
test config, which is exactly what the end-to-end tests do.

What it deliberately models from the real product:
  /pod/login ................ login form, sign-in marker
  /pod/dashboard ............. sidebar (Episodes, New episode button)
  /pod/dashboard/episodes .... episode rows with title + status chip + link
  /pod/dashboard/episode/new.  file input, "Select a file"
  .../details ............... title, rich-text description, Content checks,
                               Additional details (season/episode), Publish
                               date (Now | Schedule + date/time), Publish
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

__all__ = ["MockSpotify", "MockEpisode"]

LOGIN_URL = "/pod/login"
DASHBOARD_URL = "/pod/dashboard"
EPISODES_URL = "/pod/dashboard/episodes"


@dataclass
class MockEpisode:
    episode_id: str
    title: str = ""
    description: str = ""
    state: str = "draft"
    season: Optional[int] = None
    episode_number: Optional[int] = None
    is_explicit: bool = False
    scheduled_for: str = ""
    published_at: str = ""
    filename: str = ""
    size: int = 0
    publish_mode: str = "now"
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.signed_in = True
        self.requires_verification = False
        self.episodes: dict[str, MockEpisode] = {}
        self.draft: dict = {}
        self.session = ""
        #: Drafts and form errors keyed by the client's session id.  A single
        #: global draft would let two concurrent publishes clobber each other,
        #: which no real server does.
        self.drafts: dict[str, dict] = {}
        self.log: list[str] = []
        self.upload_delay = 0.0
        self.processing_delay = 0.0
        self.fail_upload: bool = False
        #: When set, the next publish click raises this error banner.
        self.form_error: str = ""
        self.shows: list[dict] = [{"id": "show-1", "name": "Test Show"}]

    def draft_for(self, key: str) -> dict:
        return self.drafts.setdefault(key or "default", {})

    def note(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
        del self.log[:-500]


class MockSpotify:
    """Starts the mock S4C server on a background thread."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0,
                 processing_delay: float = 0.0):
        self.state = _State()
        self.state.processing_delay = processing_delay
        handler = _make_handler(self.state, self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self.host, self.port = self._server.server_address[:2]
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "MockSpotify":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockSpotify":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- test helpers ----------------------------------------------------
    def reset(self) -> None:
        with self.state.lock:
            self.state.episodes.clear()
            self.state.drafts.clear()
            self.state.signed_in = True
            self.state.form_error = ""
            self.state.log.clear()

    def seed_episode(self, title: str, *, state: str = "published",
                     season: int | None = None, episode_number: int | None = None) -> MockEpisode:
        episode = MockEpisode(
            episode_id=uuid.uuid4().hex[:12], title=title, state=state,
            season=season, episode_number=episode_number,
            published_at="2026-09-20T10:00:00+00:00" if state == "published" else "",
        )
        with self.state.lock:
            self.state.episodes[episode.episode_id] = episode
        return episode

    def episodes(self) -> list[MockEpisode]:
        with self.state.lock:
            return list(self.state.episodes.values())


# ---------------------------------------------------------------- rendering

_PAGE = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<title>{title}</title></head><body>
{body}</body></html>"""


def _login_page(state: _State, message: str = "") -> str:
    if state.requires_verification:
        body = f"""
        <main data-testid="verify-email">
          <h1>Verify your email</h1>
          <p>We sent you an email. Verify your email to publish your first episode.</p>
        </main>"""
        return _PAGE.format(title="Verify", body=body)
    if not state.signed_in:
        body = f"""
        <main id="login-page">
          <h1>Log in to Spotify</h1>
          {f'<p role="alert">{message}</p>' if message else ''}
          <form method="post" action="{LOGIN_URL}">
            <input name="login" type="email" placeholder="Email">
            <input name="password" type="password" placeholder="Password">
            <button type="submit">Log in</button>
          </form>
        </main>"""
        return _PAGE.format(title="Log in", body=body)
    return ""


def _sidebar(state: _State) -> str:
    return f"""
    <nav data-testid="sidebar">
      <a href="{DASHBOARD_URL}/shows">Your shows</a>
      <a href="{EPISODES_URL}">Episodes</a>
    </nav>"""


def _dashboard_page(state: _State) -> str:
    shows = "".join(
        f'<li data-testid="show-item"><a href="{EPISODES_URL}?show={s["id"]}">{s["name"]}</a></li>'
        for s in state.shows
    )
    body = f"""
    {_sidebar(state)}
    <main data-testid="dashboard">
      <h1>Good evening</h1>
      <ul data-testid="show-list">{shows}</ul>
      <button data-testid="new-episode-button" onclick="location.href='/pod/dashboard/episode/new'">New episode</button>
    </main>"""
    return _PAGE.format(title="Dashboard", body=body)


def _episodes_page(state: _State) -> str:
    with state.lock:
        episodes = list(state.episodes.values())
    if not episodes:
        rows = '<p data-testid="empty-episodes">You have no episodes yet.</p>'
    else:
        rows = "".join(
            f"""<tr data-testid="episode-row" data-episode-id="{e.episode_id}"
                      data-episode-season="{(e.season if e.season is not None else '')}"
                      data-episode-number="{(e.episode_number if e.episode_number is not None else '')}">
                  <td><a data-testid="episode-title"
                         href="/pod/dashboard/episode/{e.episode_id}">{e.title}</a></td>
                  <td><span data-testid="episode-status">{e.state}</span></td>
                  <td>{e.published_at or e.scheduled_for or ""}</td>
                </tr>"""
            for e in episodes
        )
        rows = f'<table data-testid="episode-list">{rows}</table>'
    body = f"""
    {_sidebar(state)}
    <main data-testid="episodes-page">
      <h1>Episodes</h1>
      {rows}
      <button data-testid="new-episode-button" onclick="location.href='/pod/dashboard/episode/new'">New episode</button>
    </main>"""
    return _PAGE.format(title="Episodes", body=body)


def _editor_page(state: _State, episode_id: Optional[str] = None,
                 draft_key: str = "") -> str:
    with state.lock:
        draft = dict(state.draft_for(draft_key))
        draft_error = draft.get("form_error", "")
        if not draft_error and draft.get("ready") and state.form_error:
            # A test can arm a site-wide rejection banner.
            draft_error = state.form_error
    processing = "true" if state.processing_delay > 0 and not draft.get("ready") else "false"
    body = f"""
    {_sidebar(state)}
    <main data-testid="editor" data-processing="{processing}">
      <form data-testid="episode-form" method="post" action="/publish">
        <input type="hidden" name="episode_id" value="{episode_id or ''}">
        <div data-testid="upload-area">
          <label>Media file
            <input type="file" data-testid="file-input" name="file">
          </label>
          <button type="button" data-testid="select-file-button">Select a file</button>
        </div>
        <label>Title
          <input data-testid="title-input" name="title" value="{_esc(draft.get('title',''))}">
        </label>
        <label>Description
          <div contenteditable="true" role="textbox" data-testid="description-input"
               name="description">{_esc(draft.get('description',''))}</div>
          <button type="button" data-testid="html-toggle">HTML</button>
        </label>
        <fieldset data-testid="content-checks">
          <legend>Content checks</legend>
          <label><input type="radio" name="explicit" value="false"
                 {'checked' if draft.get('explicit') is False else ''}> Clean</label>
          <label><input type="radio" name="explicit" value="true"
                 {'checked' if draft.get('explicit') else ''}> Explicit</label>
        </fieldset>
        <details data-testid="additional-details">
          <summary>Additional details</summary>
          <label>Season <input data-testid="season-input" name="season"
                 value="{_esc(str(draft.get('season') or ''))}"></label>
          <label>Episode <input data-testid="episode-number-input" name="episode_number"
                 value="{_esc(str(draft.get('episode_number') or ''))}"></label>
        </details>
        <fieldset data-testid="publish-date">
          <legend>Publish date</legend>
          <label><input type="radio" data-testid="publish-now" name="publish_date" value="now"
                 {'checked' if draft.get('publish_mode','now') == 'now' else ''}> Now</label>
          <label><input type="radio" data-testid="publish-schedule" name="publish_date" value="schedule"
                 {'checked' if draft.get('publish_mode') == 'schedule' else ''}> Schedule</label>
          <input data-testid="schedule-date" type="date" name="publish_date_value"
                 value="{_esc(draft.get('schedule_date',''))}">
          <input data-testid="schedule-time" type="time" name="publish_time"
                 value="{_esc(draft.get('schedule_time',''))}">
        </fieldset>
        {f'<p role="alert" data-testid="form-error">{_esc(draft_error)}</p>' if draft_error else ''}
        <button type="submit" data-testid="publish-button">Publish</button>
        <button type="button" data-testid="save-draft-button">Save draft</button>
        <button type="button" data-testid="close-button"
                onclick="location.href='{EPISODES_URL}'">X</button>
      </form>
      {f'<p data-testid="processing">Processing your media…</p>' if processing == 'true' else ''}
    </main>
    <script>
      document.querySelector('[data-testid="episode-form"]').addEventListener('submit', e => {{
        e.preventDefault();
        const f = new FormData(e.target);
        const desc = document.querySelector('[data-testid="description-input"]').innerText;
        f.set('description', desc);
        fetch('/publish', {{method: 'POST', body: new URLSearchParams(f)}})
          .then(r => r.text()).then(html => {{
            if (html) {{ document.open(); document.write(html); document.close(); }}
          }});
      }});
    </script>"""
    return _PAGE.format(title="New episode", body=body)


def _esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------------ handler

def _make_handler(state: _State, server: MockSpotify):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence stderr noise
            return

        # -- helpers -----------------------------------------------------
        def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8",
                  extra: dict | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _drain_upload(self, length: int) -> tuple[str, int]:
            """Consume a multipart body in bounded chunks.

            Only the head of the body is kept, because the file name is the
            only part this stand-in needs.  Holding a multi-gigabyte upload in
            memory is exactly the mistake it exists to catch.
            """
            head = b""
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(_UPLOAD_CHUNK, remaining))
                if not block:
                    break
                if len(head) < 8192:
                    head += block[: 8192 - len(head)]
                remaining -= len(block)
            return _filename_from_multipart(head), length

        def _form(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)
            key = self.headers.get("X-Session-Id") or ""
            with state.lock:
                if not state.signed_in and path != LOGIN_URL:
                    self._redirect(LOGIN_URL)
                    return
                if path == LOGIN_URL:
                    self._send(_login_page(state))
                    return
                if path == "/favicon.ico":
                    self._send("", 204)
                    return
                if path == DASHBOARD_URL:
                    self._send(_dashboard_page(state))
                    return
                if path == EPISODES_URL:
                    self._send(_episodes_page(state))
                    return
                if path == "/pod/dashboard/episode/new":
                    state.drafts.pop(key, None)
                    self._send(_editor_page(state, draft_key=key))
                    return
                if path.startswith("/pod/dashboard/episode/"):
                    episode_id = path.rsplit("/", 1)[-1]
                    episode = state.episodes.get(episode_id)
                    if episode is None:
                        self._send(_PAGE.format(title="Not found", body="<p>Not found</p>"), 404)
                        return
                    state.draft_for(key).update({
                        "title": episode.title, "description": episode.description,
                        "season": episode.season, "episode_number": episode.episode_number,
                        "explicit": episode.is_explicit, "ready": True,
                        "form_error": "",
                    })
                    self._send(_editor_page(state, episode_id, draft_key=key))
                    return
                if path == "/healthz":
                    self._send(json.dumps({"ok": True}), ctype="application/json")
                    return
                if path == "/debug/log":
                    self._send(json.dumps(state.log, indent=2), ctype="application/json")
                    return
            self._send(_PAGE.format(title="404", body="<p>Not found</p>"), 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            key = self.headers.get("X-Session-Id") or ""
            with state.lock:
                if path == LOGIN_URL:
                    form = self._form()
                    if form.get("login"):
                        state.signed_in = True
                        state.note("signed in")
                        self._redirect(DASHBOARD_URL)
                    else:
                        self._send(_login_page(state, "Enter your email address."))
                    return

                if path == "/upload":
                    if state.fail_upload:
                        state.note("upload rejected by the mock")
                        self._send(
                            _PAGE.format(title="Upload failed", body=(
                                '<p role="alert" data-testid="upload-error">'
                                'Upload failed: the file could not be read.</p>')),
                            400)
                        return
                    # A real server streams the body.  Reading it all into
                    # memory would both be unrealistic and hide any buffering
                    # mistake in the code under test.
                    length = int(self.headers.get("Content-Length") or 0)
                    filename, size = self._drain_upload(length)
                    state.draft_for(key).update({
                        "filename": filename, "size": size,
                        "ready": True, "processing_seconds": state.processing_delay,
                        "form_error": "",
                    })
                    state.note(f"uploaded {filename} ({size} bytes)")
                    if state.processing_delay:
                        time.sleep(state.processing_delay)
                    self._send(_editor_page(state, draft_key=key))
                    return

                if path == "/publish":
                    form = self._form()
                    title = form.get("title", "").strip()
                    if not title:
                        state.draft_for(key)["form_error"] = "Title is required."
                        self._send(_editor_page(state, draft_key=key))
                        return
                    description = form.get("description", "")
                    explicit = form.get("explicit", "false") == "true"
                    publish_date = form.get("publish_date", "now")
                    schedule_date = form.get("publish_date_value", "")
                    schedule_time = form.get("publish_time", "")
                    draft = state.draft_for(key)
                    draft["form_error"] = ""
                    if publish_date == "schedule" and not (schedule_date and schedule_time):
                        draft["form_error"] = "Pick a publish date and time."
                        self._send(_editor_page(state, draft_key=key))
                        return
                    episode_id = uuid.uuid4().hex[:12]
                    episode = MockEpisode(
                        episode_id=episode_id,
                        title=title,
                        description=description,
                        state="scheduled" if publish_date == "schedule" else "published",
                        season=int(form["season"]) if form.get("season", "").isdigit() else None,
                        episode_number=(int(form["episode_number"])
                                        if form.get("episode_number", "").isdigit() else None),
                        is_explicit=explicit,
                        scheduled_for=f"{schedule_date}T{schedule_time}" if publish_date == "schedule" else "",
                        published_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                        if publish_date != "schedule" else "",
                        filename=draft.get("filename", ""),
                        size=draft.get("size", 0),
                        publish_mode=publish_date,
                    )
                    state.episodes[episode_id] = episode
                    state.drafts.pop(key, None)
                    state.note(f"published {title!r} as {episode.state}")
                    self._send(_episodes_page(state))
                    return
            self._send(_PAGE.format(title="404", body="<p>Not found</p>"), 404)

    return Handler


#: How much of an upload is read at a time while discarding it.
_UPLOAD_CHUNK = 256 * 1024


def _filename_from_multipart(data: bytes) -> str:
    import re

    match = re.search(rb'filename="([^"]+)"', data[:4096])
    return match.group(1).decode("utf-8", "replace") if match else "upload.bin"
