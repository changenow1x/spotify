"""API route handlers.

Security posture
----------------
* The server binds to localhost; a shared token guards every *mutating* call so
  a random web page cannot drive it (basic DNS-rebinding / CSRF defence).
* All user-supplied strings are escaped on the way to the HTML dashboard.
* Paths returned to the client are never used to build shell commands.
* No endpoint ever returns a token, cookie or password.
"""

from __future__ import annotations

import hmac
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from ..publishers.registry import known_adapters
from ..security.redaction import redact_text
from ..security.paths import PathSafetyError, is_spotify_url, is_within

router = APIRouter(prefix="/api")


def get_service(request: Request):
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="The automation service is not running.")
    return service


def require_token(
    request: Request,
    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
) -> None:
    """Guard mutating endpoints with the local shared token."""
    get_service(request)
    expected = getattr(request.app.state, "api_token", "")
    if not expected:
        return
    provided = x_api_token or request.query_params.get("token", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=403,
            detail=("This action needs the local dashboard token. Open the dashboard "
                    "at the address printed in the console and use the buttons there."),
        )


# -- status -----------------------------------------------------------------

@router.get("/status")
def get_status(service=Depends(get_service)) -> dict:
    return service.status()


@router.get("/shows")
def get_shows(service=Depends(get_service)) -> list[dict]:
    return service.shows_status()


@router.get("/jobs")
def get_jobs(
    show_id: str = Query(default=""),
    state: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    service=Depends(get_service),
) -> list[dict]:
    return service.jobs(show_id=show_id, state=state, limit=limit)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, service=Depends(get_service)) -> dict:
    job = service.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return {**service.job_view(job), "events": service.db.get_events(job_id, limit=200)}


@router.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, service=Depends(get_service)) -> list[dict]:
    return service.db.get_events(job_id, limit=300)


@router.get("/events")
def get_events(limit: int = Query(default=80, ge=1, le=400), service=Depends(get_service)) -> list[dict]:
    return service.events(limit=limit)


@router.get("/logs")
def get_logs(lines: int = Query(default=200, ge=1, le=2000), service=Depends(get_service)) -> dict:
    return {"lines": service.log_tail(lines)}


@router.get("/activity")
def get_activity(service=Depends(get_service)) -> dict:
    return {"jobs": service.db.recent_events(limit=100), "watcher": service.watcher.recent_events(100)}


@router.get("/adapters")
def get_adapters() -> list[dict]:
    return known_adapters()


@router.get("/diagnostics/selectors")
def get_selector_diagnostics() -> dict:
    from ..publishers.spotify.selectors import SELECTORS, validate_registry

    return {
        "ok": not validate_registry(),
        "problems": validate_registry(),
        "fields": {
            name: {
                "description": s.description,
                "state": s.state,
                "optional": s.optional,
                "candidates": list(s.all()),
            }
            for name, s in sorted(SELECTORS.items())
        },
    }


# -- actions ----------------------------------------------------------------

@router.post("/queue/pause", dependencies=[Depends(require_token)])
def pause_queue(service=Depends(get_service)) -> dict:
    return {"paused": service.set_paused(True)}


@router.post("/queue/resume", dependencies=[Depends(require_token)])
def resume_queue(service=Depends(get_service)) -> dict:
    return {"paused": service.set_paused(False)}


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(require_token)])
def retry_job(job_id: str, service=Depends(get_service)) -> dict:
    job = service.retry_job(job_id)
    if job is None:
        existing = service.db.get_job(job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="No such job.")
        # The job exists but is finished; saying "no such job" would be a lie.
        raise HTTPException(
            status_code=409,
            detail=f"Job is {existing['state']} and cannot be retried.",
        )
    return service.job_view(job)


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
def cancel_job(job_id: str, service=Depends(get_service)) -> dict:
    if not service.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="No such job.")
    return {"cancel_requested": True, "id": job_id}


@router.post("/jobs/{job_id}/force", dependencies=[Depends(require_token)])
def force_job(job_id: str, service=Depends(get_service)) -> dict:
    job = service.force_publish(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return service.job_view(job)


@router.post("/jobs/{job_id}/archive", dependencies=[Depends(require_token)])
def archive_job(job_id: str, service=Depends(get_service)) -> dict:
    if not service.archive_now(job_id):
        raise HTTPException(
            status_code=409,
            detail="Only a published or scheduled job that has not been archived yet can be archived.",
        )
    return service.job_view(service.db.get_job(job_id))


@router.post("/spotify/connect", dependencies=[Depends(require_token)])
def connect_spotify(service=Depends(get_service)) -> dict:
    """Open a real browser window so the user can sign in.

    The password is typed by the user into Spotify's own form; this process
    never sees it.  The flow runs on a background thread and the dashboard
    polls ``/api/spotify/status`` for the outcome.
    """
    from ..publishers.spotify.publisher import SpotifyPublisher

    publisher = service._adapter_or_none()
    if not isinstance(publisher, SpotifyPublisher):
        raise HTTPException(status_code=501, detail="The Spotify adapter is not available.")
    if service.spotify_login_running():
        return {"started": True, "already_running": True,
                "message": "A sign-in window is already open."}
    return service.start_spotify_login()


@router.post("/spotify/disconnect", dependencies=[Depends(require_token)])
def disconnect_spotify(service=Depends(get_service)) -> dict:
    from ..publishers.spotify.publisher import SpotifyPublisher

    try:
        publisher = service._adapter_or_none()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=redact_text(str(exc))) from exc
    if not isinstance(publisher, SpotifyPublisher):
        raise HTTPException(status_code=501, detail="The active adapter is not the Spotify adapter.")
    return publisher.disconnect()


@router.get("/spotify/status", dependencies=[Depends(require_token)])
def spotify_status(service=Depends(get_service)) -> dict:
    publisher = service._adapter_or_none()
    if publisher is None:
        raise HTTPException(status_code=501, detail="The Spotify adapter is not available.")
    return {
        "status": publisher.get_status().as_dict(),
        "session": publisher.auth.session_info(),
        "login": service.spotify_login_state(),
    }


@router.get("/spotify/doctor")
def spotify_doctor(live: bool = Query(default=False), service=Depends(get_service)) -> dict:
    from ..publishers.spotify.diagnostics import run_doctor

    return run_doctor(service.config, live=live).as_dict()


# -- local conveniences -----------------------------------------------------

@router.get("/open-folder")
def open_folder(path: str, service=Depends(get_service)) -> dict:
    """Reveal a folder in the OS file browser.

    The path is validated against the installation root, and it is passed to
    the OS as a *list* argument - never through a shell.
    """
    root = service.config.root_path()
    try:
        if not is_within(root, path):
            raise PathSafetyError(
                "That folder is outside this installation, so it cannot be opened."
            )
    except PathSafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target = Path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="That folder does not exist.")
    if not target.is_dir():
        # Otherwise os.startfile would open the file in its default program,
        # which is never what "reveal this folder" means.
        raise HTTPException(status_code=400, detail="Only folders can be opened here.")
    return _reveal(target)


@router.get("/open-url")
def open_url(url: str, service=Depends(get_service)) -> dict:
    """Open an https Spotify episode link in the default browser.

    The URL is parsed and checked host by host. A plain ``startswith`` would
    accept ``https://open.spotify.com/\nhttps://elsewhere.example`` and
    ``https://open.spotify.com.elsewhere.example``.
    """
    if not is_spotify_url(url):
        raise HTTPException(
            status_code=400,
            detail="Only Spotify links are opened from the dashboard.",
        )
    return _launch(url)


def _reveal(path: Path) -> dict:
    import sys as _sys

    try:
        if _sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - documented Windows API
            return {"opened": True, "how": "explorer"}
        if _sys.platform == "darwin":
            subprocess.run(["open", str(path)], shell=False, check=True)  # noqa: S603,S607
            return {"opened": True, "how": "open"}
        subprocess.run(["xdg-open", str(path)], shell=False, check=True)  # noqa: S603,S607
        return {"opened": True, "how": "xdg-open"}
    except Exception as exc:  # noqa: BLE001
        return {"opened": False, "error": redact_text(str(exc)),
                "hint": "Open the folder manually; the path is shown above."}


def _launch(url: str) -> dict:
    import webbrowser

    try:
        opened = webbrowser.open(url)
        return {"opened": bool(opened), "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"opened": False, "error": redact_text(str(exc)), "url": url}
