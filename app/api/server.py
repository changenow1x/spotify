"""The local dashboard server.

FastAPI + uvicorn, bound to localhost.  The frontend is a single static page
with no build step and no CDN, so the dashboard works with the internet
unplugged - which is the whole point of a local-first automation tool.
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..security.redaction import redact_text
from .routes import router

log = logging.getLogger(__name__)

__all__ = ["create_app", "run_server", "DashboardServer", "find_free_port"]

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def create_app(service, *, api_token: str | None = None) -> FastAPI:
    """Build the FastAPI application bound to *service*."""
    app = FastAPI(
        title="PodcastAutomation",
        description="Local control panel for automatic Spotify publishing.",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.service = service
    app.state.api_token = api_token if api_token is not None else _load_or_create_token(service)
    app.include_router(router)

    if DASHBOARD_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(DASHBOARD_DIR / "index.html")

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            path = DASHBOARD_DIR / "favicon.svg"
            if path.is_file():
                return FileResponse(path, media_type="image/svg+xml")
            return JSONResponse({}, status_code=204)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover
        log.exception("Unhandled API error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": redact_text(str(exc))},
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}
    return app


def _load_or_create_token(service) -> str:
    """A per-installation local token, stored in the database (not a secret)."""
    db = getattr(service, "db", None)
    if db is None:
        return ""
    token = db.get_setting("api.token")
    if not token:
        token = secrets.token_urlsafe(24)
        db.set_setting("api.token", token)
    return token


def find_free_port(host: str, preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, preferred))
            return preferred
        except OSError:
            sock.bind((host, 0))
            return sock.getsockname()[1]


class DashboardServer:
    """Runs uvicorn on a background thread."""

    def __init__(self, service, *, host: str = "127.0.0.1", port: int = 8765,
                 open_browser: bool = False):
        self.service = service
        self.host = host
        self.port = port
        self.open_browser = open_browser
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self.token = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self, wait: float = 10.0) -> str:
        import uvicorn

        self.port = find_free_port(self.host, self.port)
        app = create_app(self.service)
        self.token = app.state.api_token
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning",
            access_log=False, lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="dashboard", daemon=True)
        self._thread.start()
        self._started.wait(wait)
        log.info("Dashboard available at %s", self.url)
        if self.open_browser:
            self._open()
        return self.url

    def _open(self) -> None:
        url = self.url
        if self.token:
            url = f"{url}#token={self.token}"
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001  # pragma: no cover
            log.info("Open %s in your browser to see the dashboard.", self.url)

    def stop(self, timeout: float = 8.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None


def run_server(service, *, host: str = "127.0.0.1", port: int = 8765,
               open_browser: bool = False) -> DashboardServer:
    """Blocking dashboard run (used by ``python -m app --dashboard``)."""
    server = DashboardServer(service, host=host, port=port, open_browser=open_browser)
    server.start()
    try:
        while server._thread and server._thread.is_alive():
            server._thread.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return server
