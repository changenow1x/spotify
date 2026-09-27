"""Regression: a finished, published job must not be resurrected by retry-now."""

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.core.service import RETRYABLE_STATES
from app.core.state_machine import JobState


def _published(harness):
    harness.drop_media("Episode One")
    harness.drop_metadata("Episode One", TITLE="Episode One", SEASON=1, EPISODE=1)
    job_id = harness.discover()[0]["id"]
    harness.drain()
    assert harness.service.db.get_job(job_id)["state"] == "ARCHIVED"
    return job_id


def test_a_finished_job_is_not_retryable(harness):
    for state in (JobState.ARCHIVED, JobState.PUBLISHED, JobState.SCHEDULED):
        assert state not in RETRYABLE_STATES


def test_retryable_states_are_the_recoverable_ones(harness):
    for state in (JobState.FAILED, JobState.RETRY_WAIT, JobState.NEEDS_USER_ACTION,
                  JobState.NEEDS_RECONCILIATION, JobState.CANCELLED):
        assert state in RETRYABLE_STATES


def test_a_failed_job_is_still_retryable(harness):
    """The guard must not block the case the control exists for."""
    # Unparseable media makes validation fail, which is the normal reason a
    # user reaches for "Retry now".
    harness.incoming.mkdir(parents=True, exist_ok=True)
    (harness.incoming / "Broken.mp3").write_bytes(b"not audio at all")
    harness.drop_metadata("Broken", TITLE="Broken", SEASON=1, EPISODE=1)
    job_id = harness.discover()[0]["id"]
    harness.drain()
    failed = harness.service.db.get_job(job_id)["state"]
    assert failed in {"FAILED", "NEEDS_USER_ACTION"}, failed
    assert failed in {s.value for s in RETRYABLE_STATES}

    assert harness.service.retry_job(job_id) is not None
    assert harness.service.db.get_job(job_id)["state"] == "RETRY_WAIT"


def test_a_queued_job_is_not_retryable(harness):
    """DISCOVERED is not a recovery state; retrying it would skip validation."""
    harness.drop_media("Fresh")
    harness.drop_metadata("Fresh", TITLE="Fresh", SEASON=1, EPISODE=1)
    job_id = harness.discover()[0]["id"]
    assert harness.service.db.get_job(job_id)["state"] == "DISCOVERED"
    assert harness.service.retry_job(job_id) is None
    assert harness.service.db.get_job(job_id)["state"] == "DISCOVERED"


def test_retry_now_cannot_resurrect_a_published_job(harness):
    harness.drop_media("Episode One")
    harness.drop_metadata("Episode One", TITLE="Episode One", SEASON=1, EPISODE=1)
    job_id = harness.discover()[0]["id"]
    harness.drain()
    job = harness.service.db.get_job(job_id)
    assert job["state"] == "ARCHIVED", job["state"]
    assert job["spotify_episode_id"]
    assert job["publish_confirmed_at"]

    result = harness.service.retry_job(job_id)

    after = harness.service.db.get_job(job_id)
    assert result is None, f"retry_job must refuse a published job, got {result}"
    assert after["state"] == "ARCHIVED", f"published job regressed to {after['state']}"
    assert after["spotify_episode_id"] == job["spotify_episode_id"]
    assert after["publish_confirmed_at"] == job["publish_confirmed_at"]

    # And a second drain must not create a second remote episode.
    before = len(harness.spotify.episodes())
    harness.drain()
    assert len(harness.spotify.episodes()) == before
    assert harness.service.db.get_job(job_id)["state"] == "ARCHIVED"


def test_retry_api_reports_a_conflict_not_a_missing_job(harness):
    job_id = _published(harness)
    client = TestClient(create_app(harness.service, api_token="t"))
    client.headers.update({"X-API-Token": "t"})
    response = client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 409, response.text
    assert "cannot be retried" in response.json()["detail"].lower()
    assert harness.service.db.get_job(job_id)["state"] == "ARCHIVED"


def test_retry_api_still_404s_for_a_genuinely_unknown_job(harness):
    client = TestClient(create_app(harness.service, api_token="t"))
    client.headers.update({"X-API-Token": "t"})
    assert client.post("/api/jobs/does-not-exist/retry").status_code == 404
