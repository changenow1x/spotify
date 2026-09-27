"""Dashboard HTTP API tests.

These exercise the real FastAPI application with the real service container and
a real (temporary) SQLite database, so the routes are covered end to end
without needing a browser.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.api.server import create_app
from app.core.database import JobState
from app.security.paths import PathSafetyError

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def client(harness):
    service = harness.service
    app = create_app(service, api_token="test-token")
    with fastapi_testclient.TestClient(app) as test_client:
        test_client.headers.update({"X-API-Token": "test-token"})
        yield test_client


def _drop(harness, name="Episode One", **kwargs):
    return harness.drop_media(name, **kwargs)


# --------------------------------------------------------------- basics ---
class TestReadEndpoints:
    def test_healthz(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_dashboard_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_status_reports_worker_state(self, client, harness):
        response = client.get("/api/status")
        assert response.status_code == 200
        body = response.json()
        assert body["workers"], "the dashboard must show at least one worker"
        assert body["shows"], "the dashboard must list the configured shows"
        for worker in body["workers"]:
            assert {"id", "alive", "paused", "current_state"} <= set(worker)

    def test_status_marks_workers_paused_when_the_queue_is_paused(self, client, harness):
        harness.config.pause_queue = True
        try:
            workers = client.get("/api/status").json()["workers"]
        finally:
            harness.config.pause_queue = False
        assert all(w["paused"] for w in workers)

    def test_shows_endpoint(self, client, harness):
        body = client.get("/api/shows").json()
        assert {s["name"] for s in body} == {s.name for s in harness.config.shows}

    def test_jobs_listing(self, client, harness):
        _drop(harness, "Listed")
        job = harness.discover_one()
        rows = client.get("/api/jobs").json()
        ids = [j["id"] for j in rows]
        assert job.id in ids
        first = next(j for j in rows if j["id"] == job.id)
        assert first["state"] in {s.value for s in JobState}

    def test_job_detail_and_events(self, client, harness):
        _drop(harness, "Detailed")
        job = harness.discover_one()
        detail = client.get(f"/api/jobs/{job.id}").json()
        assert detail["id"] == job.id
        assert detail["events"], "a job must carry its event history"

        events = client.get(f"/api/jobs/{job.id}/events").json()
        assert events, "a discovered job must have at least one event"

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/jobs/does-not-exist").status_code == 404

    def test_logs_endpoint(self, client):
        body = client.get("/api/logs", params={"lines": 5}).json()
        assert "lines" in body

    def test_activity_endpoint(self, client):
        body = client.get("/api/activity").json()
        assert "jobs" in body and "watcher" in body

    def test_adapters_endpoint(self, client):
        body = client.get("/api/adapters").json()
        assert any(a["name"] == "spotify" for a in body)
        assert any(a["status"] == "implemented" for a in body)

    def test_diagnostics_selectors(self, client):
        body = client.get("/api/diagnostics/selectors").json()
        assert body["fields"], "the selector registry must be exposed"
        assert body["ok"] is True


# ------------------------------------------------------------------ auth ---
class TestTokenProtection:
    MUTATIONS = [
        ("post", "/api/queue/pause", {}),
        ("post", "/api/queue/resume", {}),
        ("post", "/api/jobs/x/retry", {}),
        ("post", "/api/jobs/x/cancel", {}),
        ("post", "/api/jobs/x/force", {}),
        ("post", "/api/spotify/connect", {}),
        ("post", "/api/spotify/disconnect", {}),
    ]

    @pytest.mark.parametrize("method,path,payload", MUTATIONS)
    def test_mutation_requires_token(self, harness, method, path, payload):
        app = create_app(harness.service, api_token="secret")
        with fastapi_testclient.TestClient(app) as anonymous:
            response = getattr(anonymous, method)(path, json=payload)
        assert response.status_code in (401, 403), f"{path} was reachable without a token"

    @pytest.mark.parametrize("method,path,payload", MUTATIONS)
    def test_wrong_token_is_rejected(self, harness, method, path, payload):
        app = create_app(harness.service, api_token="secret")
        with fastapi_testclient.TestClient(app) as wrong:
            wrong.headers["X-API-Token"] = "guess"
            response = getattr(wrong, method)(path, json=payload)
        assert response.status_code in (401, 403)

    def test_token_is_persisted_per_installation(self, harness):
        first = create_app(harness.service, api_token=None).state.api_token
        second = create_app(harness.service, api_token=None).state.api_token
        assert first and first == second, "the token must survive a restart"


# --------------------------------------------------------------- actions ---
class TestDashboardActions:
    def test_retry_a_failed_job(self, client, harness):
        _drop(harness, "Retry Me")
        job = harness.discover_one()
        harness.db.force_state(job.id, JobState.FAILED, message="boom")
        response = client.post(f"/api/jobs/{job.id}/retry", json={"now": True})
        assert response.status_code == 200
        assert harness.db.get_job(job.id).state_enum() in {
            JobState.QUEUED, JobState.RETRY_WAIT
        }

    def test_cancel_a_job(self, client, harness):
        _drop(harness, "Cancel Me")
        job = harness.discover_one()
        response = client.post(f"/api/jobs/{job.id}/cancel")
        assert response.status_code == 200
        row = harness.db.get_job(job.id)
        assert row.cancel_requested is True or row.state_enum() == JobState.CANCELLED

    def test_pause_and_resume(self, client, harness):
        assert client.post("/api/queue/pause").status_code == 200
        assert client.get("/api/status").json()["app"]["queue_paused"] is True
        assert client.post("/api/queue/resume").status_code == 200
        assert client.get("/api/status").json()["app"]["queue_paused"] is False

    def test_retry_unknown_job_is_404(self, client):
        assert client.post("/api/jobs/nope/retry").status_code == 404

    def test_spotify_status_requires_a_session(self, client):
        response = client.get("/api/spotify/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"]["ok"] is False
        assert "session" in body and "login" in body


# ------------------------------------------------------------ OS helpers ---
class TestOpenHelpersAreSafe:
    def test_open_folder_rejects_traversal(self, client, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        response = client.get("/api/open-folder", params={"path": str(outside)})
        assert response.status_code == 400
        assert "outside this installation" in response.json()["detail"]

    def test_open_folder_rejects_dotdot(self, client, harness):
        sneaky = harness.config.root_path() / ".." / ".."
        response = client.get("/api/open-folder", params={"path": str(sneaky)})
        assert response.status_code == 400

    def test_open_folder_rejects_a_file(self, client, harness):
        media = harness.drop_media("Just A File")
        response = client.get("/api/open-folder", params={"path": str(media)})
        assert response.status_code in (400, 404)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://open.spotify.com/x",
            "https://evil.example.com/",
            "https://open.spotify.com.evil.example/",
            "javascript:alert(1)",
        ],
    )
    def test_open_url_only_allows_spotify(self, client, url):
        response = client.get("/api/open-url", params={"url": url})
        assert response.status_code == 400, f"{url!r} was not rejected"

    @pytest.mark.parametrize(
        "url",
        [
            "https://open.spotify.com/\nhttps://evil.example",
            "https://open.spotify.com/\r\nHost: evil.example",
            "https://open.spotify.com/ https://evil.example",
            "https://user:pw@open.spotify.com/episode",
        ],
    )
    def test_open_url_rejects_control_characters(self, client, url):
        """A newline must not let a second URL ride along behind a valid prefix."""
        response = client.get("/api/open-url", params={"url": url})
        assert response.status_code == 400, f"{url!r} was not rejected"

    def test_open_url_rejects_a_spotify_prefix_bypass(self, client, monkeypatch):
        """A path traversal on the Spotify origin must not open a local file."""
        called: list[str] = []
        monkeypatch.setattr(
            "app.api.routes._launch", lambda url: called.append(url) or {"opened": True}
        )
        response = client.get(
            "/api/open-url",
            params={"url": "https://open.spotify.com/../../../../etc/passwd"},
        )
        # Accepted as a Spotify link at worst, but it must never become a path.
        if response.status_code == 200:
            assert called and called[0].startswith("https://open.spotify.com/")
        else:
            assert response.status_code == 400


# ------------------------------------------------------------------ XSS ---
class TestOutputEscaping:
    """The JSON API returns stored text verbatim; the dashboard must never
    interpret it as markup. These tests pin that contract from both sides."""

    def test_stored_text_is_returned_verbatim(self, client, harness):
        hostile = '<script>alert("xss")</script>'
        job = harness.service.db.create_job(
            media_path=str(harness.incoming / "x.mp3"),
            metadata_path=None,
            show_id=harness.config.shows[0].id,
        )
        harness.service.db.force_state(job.id, JobState.FAILED, message=hostile)
        rows = client.get("/api/jobs").json()
        row = next(j for j in rows if j["id"] == job.id)
        assert row["status_message"] == hostile, (
            "the API must not silently alter stored text; escaping is the "
            "dashboard's job, and it uses textContent"
        )

    def test_dashboard_never_assigns_inner_html(self):
        source = (Path(__file__).resolve().parents[1] / "app/dashboard/app.js").read_text(
            encoding="utf-8"
        )
        # Strip comments so the file's own explanatory comment is allowed.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith(("*", "//", "/*"))
        )
        for sink in ("innerHTML =", "outerHTML =", "insertAdjacentHTML", "document.write"):
            assert sink not in code, f"the dashboard must not use {sink}"

    def test_dashboard_uses_text_content_for_untrusted_values(self):
        source = (Path(__file__).resolve().parents[1] / "app/dashboard/app.js").read_text(
            encoding="utf-8"
        )
        assert source.count("textContent") >= 15, "values should be written as text"

    def test_error_detail_does_not_leak_paths_outside_root(self, client):
        response = client.get("/api/open-folder", params={"path": "/etc"})
        assert response.status_code == 400
        assert "traceback" not in response.text.lower()

    def test_token_is_never_echoed_in_responses(self, client):
        for path in ("/api/status", "/api/jobs", "/api/spotify/status"):
            assert "test-token" not in client.get(path).text


# ------------------------------------------------------- route inventory ---
class TestRouteInventory:
    EXPECTED_READ = [
        "/api/status", "/api/shows", "/api/jobs", "/api/events", "/api/logs",
        "/api/activity", "/api/adapters", "/api/diagnostics/selectors",
    ]

    def test_every_documented_read_route_exists(self, client):
        for path in self.EXPECTED_READ:
            assert client.get(path).status_code == 200, f"{path} is missing"

    def test_openapi_schema_builds(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/api/jobs/{job_id}/retry" in paths
        assert "post" in paths["/api/jobs/{job_id}/retry"]
