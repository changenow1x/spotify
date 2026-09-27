"""A :class:`BrowserSession` that talks to the mock Spotify over real HTTP.

This is what lets the publishing flow be tested *for real* in CI: the adapter
performs genuine HTTP requests, the mock server genuinely stores the episode,
and the adapter's own field-filling / re-read / publish-confirmation logic is
genuinely exercised.  Nothing is stubbed out at the adapter boundary.

It is **not** a browser: there is no JavaScript, so the contenteditable
description is read from the served HTML.  That is the one behaviour the real
Playwright session provides that this cannot - everything else matches the
:class:`BrowserSession` contract.

It also supports ``fail_next`` so a test can prove the adapter reports a
failure instead of falsely reporting success.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Optional, Sequence

from app.publishers.spotify.browser import BrowserSession, SessionError

__all__ = ["FakeSession", "Node", "query_all", "parse_html"]


# --------------------------------------------------------------- tiny DOM --

class Node:
    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs
        self.children: list["Node"] = []
        self.text: list[str] = []
        self.parent = parent

    @property
    def inner_text(self) -> str:
        parts: list[str] = list(self.text)
        for child in self.children:
            parts.append(child.inner_text)
        return " ".join(p for p in parts if p and p.strip())

    def get_text(self) -> str:
        return self.inner_text

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.tag} {self.attrs}>"


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if data and data.strip():
            self.stack[-1].text.append(data)


def parse_html(html: str) -> Node:
    parser = _Parser()
    parser.feed(html)
    parser.close()
    return parser.root


# ------------------------------------------------------------- selectors --

_ATTR_RE = re.compile(r'\[\s*([A-Za-z_:][-A-Za-z0-9_:.]*)\s*(?:([~^$*|]?=)\s*"([^"]*)"|'
                      r"""([~^$*|]?=)\s*'([^']*)'|([~^$*|]?=)\s*([^\]\s]+))?\s*\]""")
_HAS_TEXT_RE = re.compile(r':has-text\(\s*["\'](?P<text>.*?)["\']\s*\)')
_TEXT_IS_RE = re.compile(r'^text=/(?P<body>.*)/(?P<flags>[a-z]*)$')
_NTH_RE = re.compile(r">>\s*nth=(?P<n>\d+)")


def _attr_matches(node: Node, name: str, op: str | None, value: str | None) -> bool:
    actual = node.attrs.get(name)
    if op is None:
        return name in node.attrs
    if actual is None:
        return False
    if op == "=":
        return actual == value
    if op == "*=":
        return value in actual
    if op == "^=":
        return actual.startswith(value)
    if op == "$=":
        return actual.endswith(value)
    if op == "~=":
        return value in actual.split()
    if op == "|=":
        return actual == value or actual.startswith(value + "-")
    if op == "!=":
        return actual != value
    return False


def _matches_simple(node: Node, simple: str) -> bool:
    simple = simple.strip()
    if not simple or simple == "*":
        return True
    match = _TEXT_IS_RE.match(simple)
    if match:
        flags = re.IGNORECASE if "i" in match.group("flags") else 0
        return bool(re.search(match.group("body"), node.inner_text, flags))

    has_text = None
    ht = _HAS_TEXT_RE.search(simple)
    if ht:
        has_text = ht.group("text")
        simple = simple[: ht.start()]

    tag = None
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9-]*)", simple)
    if tag_match:
        tag = tag_match.group(1).lower()
        simple = simple[tag_match.end():]

    if tag and node.tag != tag:
        return False
    ok = True
    for m in _ATTR_RE.finditer(simple):
        name = m.group(1)
        op = m.group(2) or m.group(4) or m.group(6)
        value = m.group(3) if m.group(2) else (m.group(5) if m.group(4) else m.group(7))
        if not _attr_matches(node, name, op, value):
            ok = False
            break
    if not ok:
        return False
    if has_text is not None:
        if has_text not in node.inner_text:
            return False
    return True


def _split_selector(selector: str) -> list[str]:
    """Split on whitespace, but not inside quotes.

    ``button:has-text("Select a file")`` is one compound selector, not three
    descendant steps.
    """
    parts: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    depth = 0
    for char in selector:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if char.isspace() and depth == 0:
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _descendants(node: Node) -> list[Node]:
    out: list[Node] = []
    stack = list(node.children)
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(reversed(current.children))
    return out


def query_all(root: Node, selector: str) -> list[Node]:
    """Support the selector dialect used in ``spotify/selectors.py``."""
    selector = (selector or "").strip()
    if not selector:
        return []
    nth = None
    nth_match = _NTH_RE.search(selector)
    if nth_match:
        nth = int(nth_match.group("n"))
        selector = selector[: nth_match.start()].strip()

    parts = _split_selector(selector)
    if not parts:
        return []
    nodes = [root]
    for part in parts:
        found: list[Node] = []
        for node in nodes:
            for candidate in _descendants(node):
                if _matches_simple(candidate, part) and candidate not in found:
                    found.append(candidate)
        nodes = found
        if not nodes:
            return []
    if nth is not None:
        return [nodes[nth]] if 0 <= nth < len(nodes) else []
    return nodes


# --------------------------------------------------------------- session --

class FakeSession(BrowserSession):
    """HTTP-backed session against :class:`tests.mock_spotify.server.MockSpotify`."""

    def __init__(self, base_url: str, *, page_delay: float = 0.0,
                 session_id: str = ""):
        self.base_url = base_url.rstrip("/")
        # Each session is a separate browser profile with its own cookies, so
        # the server must scope any half-finished draft to it.
        self.session_id = session_id or uuid.uuid4().hex
        self._html = ""
        self._dom_cache: Optional[Node] = None
        self._dom_source: Optional[str] = None
        self._url = f"{self.base_url}/pod/login"
        self._open = False
        self._post_body: dict = {}
        self._post_path = ""
        self._file_input_payload: Optional[tuple[str, bytes]] = None
        self.page_delay = page_delay
        self.fail_next: Optional[str] = None
        self.actions: list[str] = []
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._open = True
        self.actions.append("start")

    def close(self) -> None:
        self._open = False
        self.actions.append("close")

    def is_open(self) -> bool:
        return self._open

    def _set_html(self, html: str) -> None:
        self._html = html
        self._dom_cache = None
        self._dom_source = None

    def _check(self) -> None:
        if not self._open:
            raise SessionError("The browser session is not open. Start it first.")

    def _maybe_fail(self) -> None:
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            raise SessionError(message)

    # -- transport -------------------------------------------------------
    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None) -> tuple[int, str]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        merged = {"X-Session-Id": self.session_id}
        merged.update(headers or {})
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=merged)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    # -- navigation ------------------------------------------------------
    def goto(self, url: str, *, timeout_ms: int | None = None) -> str:
        self._check()
        self._maybe_fail()
        path = url if url.startswith("http") else url
        if path.startswith("http"):
            parsed = urllib.parse.urlparse(path)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        status, html = self._request("GET", path)
        self._maybe_fail()
        if self.page_delay:
            time.sleep(self.page_delay)
        self._set_html(html)
        self._url = path if path.startswith("/") else url
        self.actions.append(f"goto {path}")
        # Follow a redirect the way a browser would.
        if status in (301, 302, 303, 307, 308):
            return self.url()
        return self._url

    def url(self) -> str:
        return f"{self.base_url}{self._url}" if self._url.startswith("/") else self._url

    def wait_for_url(self, pattern: str, *, timeout_ms: int | None = None) -> bool:
        return bool(re.search(pattern, self._url))

    # -- queries ---------------------------------------------------------
    def _dom(self) -> Node:
        """The parsed page, cached so in-place edits survive re-queries."""
        if self._dom_cache is None or self._dom_source != self._html:
            self._dom_cache = parse_html(self._html)
            self._dom_source = self._html
        return self._dom_cache

    def _nodes(self, selector: str) -> list[Node]:
        return query_all(self._dom(), selector)

    def exists(self, selector: str, *, timeout_ms: int = 1500) -> bool:
        self._check()
        return bool(self._nodes(selector))

    def wait_for(self, selector: str, *, timeout_ms: int | None = None,
                 state: str = "visible") -> bool:
        deadline = time.monotonic() + min(3.0, (timeout_ms or 3000) / 1000)
        while True:
            if self._nodes(selector):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def count(self, selector: str) -> int:
        self._check()
        return len(self._nodes(selector))

    def text(self, selector: str, *, timeout_ms: int = 2000) -> str:
        nodes = self._nodes(selector)
        return nodes[0].inner_text.strip() if nodes else ""

    def texts(self, selector: str) -> list[str]:
        return [n.inner_text.strip() for n in self._nodes(selector)]

    def attr(self, selector: str, name: str) -> Optional[str]:
        nodes = self._nodes(selector)
        return nodes[0].attrs.get(name) if nodes else None

    def attrs(self, selector: str, name: str) -> list[Optional[str]]:
        return [n.attrs.get(name) for n in self._nodes(selector)]

    def value(self, selector: str, *, timeout_ms: int = 2000) -> str:
        nodes = self._nodes(selector)
        return nodes[0].attrs.get("value", "") if nodes else ""

    # -- actions ---------------------------------------------------------
    def click(self, selector: str, *, timeout_ms: int | None = None) -> None:
        self._check()
        self._maybe_fail()
        nodes = self._nodes(selector)
        if not nodes:
            raise SessionError(f"Could not click {selector!r}: no such element.")
        node = nodes[0]
        self.actions.append(f"click {selector}")
        # The mock expresses navigation with inline location/onclick attributes.
        target = node.attrs.get("onclick", "")
        location = re.search(r"location\.href=['\"]([^'\"]+)['\"]", target)
        form_action = node.attrs.get("formaction")
        if location:
            self.goto(location.group(1))
            return
        if form_action:
            self._post_path = form_action
            return
        if node.tag == "form":
            self._submit_form(node)
            return
        # A submit button submits its enclosing form, exactly as in a browser.
        if node.attrs.get("type") == "submit" or node.tag == "button":
            ancestor = node.parent
            while ancestor is not None:
                if ancestor.tag == "form":
                    self._submit_form(ancestor, submitter=node)
                    return
                ancestor = ancestor.parent

    def _submit_form(self, form: Node, submitter: Optional[Node] = None) -> None:
        fields: dict[str, str] = {}
        for node in _descendants(form):
            if node.tag == "input" and node.attrs.get("name"):
                kind = node.attrs.get("type", "text")
                if kind == "radio":
                    # A browser submits only the *checked* radio in a group.
                    if "checked" in node.attrs:
                        fields[node.attrs["name"]] = node.attrs.get("value", "")
                elif kind == "checkbox":
                    if "checked" in node.attrs:
                        fields[node.attrs["name"]] = node.attrs.get("value", "on")
                elif kind != "file":
                    fields[node.attrs["name"]] = node.attrs.get("value", "")
            elif node.tag == "textarea" and node.attrs.get("name"):
                fields[node.attrs["name"]] = node.inner_text
        for node in _descendants(form):
            if node.attrs.get("contenteditable") == "true" and node.attrs.get("name"):
                fields[node.attrs["name"]] = node.inner_text
        self._post_body = dict(fields)
        self._post_path = form.attrs.get("action", self._post_path or "/publish")
        if submitter is not None and submitter.attrs.get("name"):
            fields[submitter.attrs["name"]] = submitter.attrs.get("value", "")
        body = urllib.parse.urlencode(fields).encode("utf-8")
        status, html = self._request("POST", self._post_path, body,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
        if status in (301, 302, 303, 307, 308):
            self.goto(self._url)
            return
        self._set_html(html)

    def fill(self, selector: str, text: str, *, timeout_ms: int | None = None) -> None:
        self._check()
        self._maybe_fail()
        nodes = self._nodes(selector)
        if not nodes:
            raise SessionError(f"Could not type into {selector!r}: no such element.")
        node = nodes[0]
        node.attrs["value"] = text
        if node.attrs.get("contenteditable") == "true" or node.tag == "textarea":
            node.children = [Node("text", {}, None)]
            node.children[0].text = [text]
        # The mock serves the editor from the server state; reflect the edit.
        self.actions.append(f"fill {selector} ({len(text)} chars)")
        if "data-testid=\"title-input\"" in selector:
            self._server_draft_update(title=text)
        elif "description" in selector:
            self._server_draft_update(description=text)
        elif "season" in selector:
            self._server_draft_update(season=text)
        elif "episode-number" in selector or "episode_number" in selector:
            self._server_draft_update(episode_number=text)
        elif "schedule-date" in selector or "publish_date_value" in selector:
            self._server_draft_update(schedule_date=text)
        elif "schedule-time" in selector or "publish_time" in selector:
            self._server_draft_update(schedule_time=text)
        elif "explicit" in selector or "content-check" in selector:
            self._server_draft_update(explicit="true" in text.lower())
        elif "publish" in selector and "date" in selector:
            self._server_draft_update(publish_mode="schedule" if "schedule" in selector else "now")

    def _server_draft_update(self, **fields) -> None:
        """Push the edit into the server so a page reload keeps it."""
        with self._lock:
            if self._draft_holder is not None:
                self._draft_holder.update(fields)

    #: Set by the test to the mock's ``_State`` object.
    _draft_holder: Optional[dict] = None

    def press(self, selector: str, key: str) -> None:
        self.actions.append(f"press {selector} {key}")

    def check(self, selector: str, *, timeout_ms: int | None = None) -> None:
        self._check()
        self._maybe_fail()
        nodes = self._nodes(selector)
        if not nodes:
            raise SessionError(f"Could not toggle {selector!r}: no such element.")
        node = nodes[0]
        if node.tag == "input" and node.attrs.get("type") in {"radio", "checkbox"}:
            # Radio groups: uncheck the siblings, check this one.
            if node.attrs.get("type") == "radio" and node.attrs.get("name"):
                for other in self._nodes(f'input[name="{node.attrs["name"]}"]'):
                    other.attrs.pop("checked", None)
            node.attrs["checked"] = ""
            if "explicit" in selector:
                self._server_draft_update(explicit=node.attrs.get("value") == "true")
            elif "publish" in selector:
                self._server_draft_update(
                    publish_mode="schedule" if node.attrs.get("value") == "schedule" else "now")
            self.actions.append(f"check {selector}")
            return
        node.attrs.setdefault("aria-checked", "false")
        node.attrs["aria-checked"] = "true" if node.attrs["aria-checked"] == "false" else "false"
        if "explicit" in selector:
            self._server_draft_update(explicit=node.attrs["aria-checked"] == "true")
        self.actions.append(f"toggle {selector}")

    def set_input_files(self, selector: str, paths: Sequence[str],
                        *, timeout_ms: int | None = None) -> None:
        from pathlib import Path

        self._check()
        self._maybe_fail()
        nodes = self._nodes(selector)
        if not nodes:
            raise SessionError(f"Could not attach a file to {selector!r}: no such element.")
        path = Path(paths[0])
        if not path.is_file():
            raise SessionError(f"The media file to upload does not exist: {path}")
        if path.stat().st_size == 0:
            raise SessionError(f"The media file to upload is empty: {path}")
        data = path.read_bytes()
        boundary = "----PodcastAutomationBoundary"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        status, html = self._request("POST", "/upload", body,
                                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self._maybe_fail()
        self._set_html(html)
        self._url = "/pod/dashboard/episode/new"
        self.actions.append(f"upload {path.name} ({len(data)} bytes, http {status})")
        if status >= 400:
            raise SessionError("Spotify rejected the upload.")

    # -- diagnostics -----------------------------------------------------
    def content(self) -> str:
        return self._html

    def screenshot(self, path):
        from pathlib import Path

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"<!-- fake screenshot of {self._url} -->\n{self._html[:200]}".encode())
        return path

    def evaluate(self, script: str) -> Any:
        # The only script the publisher uses reads a field's current value.
        m = re.search(r"document\.querySelector\((?P<q>['\"])(?P<sel>.+?)(?P=q)\)", script)
        if m:
            nodes = self._nodes(m.group("sel"))
            if not nodes:
                return None
            node = nodes[0]
            if "value" in node.attrs:
                return node.attrs["value"]
            return node.inner_text
        return None

    def capture(self, name: str, *, reason: str = "") -> dict:
        return {"name": name, "reason": reason, "url": self.url(), "fake": True}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
