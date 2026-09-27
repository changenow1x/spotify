"""Release gate: hard termination, reopen, and continuation.

The checklist's database/recovery gate:
  1. active upload
  2. hard process termination
  3. database reopen
  4. worker restart
  5. remote reconciliation
  6. continuation

A job must never be orphaned, permanently locked, duplicated, or silently
lost.  "Hard termination" is modelled by abandoning the connection and the
worker without any orderly shutdown - the closest a test can get to pulling
the power, since SIGKILL cannot run any cleanup.
"""

from __future__ import annotations

import gc
import sqlite3

import pytest

from app.core.database import Database
from app.core.state_machine import JobState
from tests.harness import GOOD_METADATA, Harness


def _reopen(harness: Harness) -> Database:
    """Close and reopen the database file, as a restart would."""
    path = harness.db.path
    harness.db.close()
    gc.collect()
    return Database(path)


def test_an_interrupted_upload_is_recovered_and_published_once(harness: Harness):
    """Steps 1-6: kill mid-upload, reopen, recover, finish - exactly once."""
    harness.drop_pair("UP", kind="mp3")
    jobs = harness.discover()
    job_id = jobs[0]["id"]

    # 1. Reach the point where the job is genuinely in flight.
    original = harness.worker.adapter.publish
    reached = {"done": False}

    def killed(request, **kwargs):
        reached["done"] = True
        raise SystemExit("power cut")

    harness.worker.adapter.publish = killed
    with pytest.raises(SystemExit):
        harness.worker.run_once()
    harness.worker.adapter.publish = original
    assert reached["done"], "the run never reached the upload"

    # 2 + 3. Hard termination and reopen: drop the worker and the connection.
    worker = harness.worker
    del worker
    gc.collect()
    db = _reopen(harness)

    # The job survived the file being reopened - it is not orphaned.
    row = db.get_job(job_id)
    assert row is not None, "the job was lost when the database was reopened"
    assert row.state_enum() is not JobState.ARCHIVED

    # 4 + 5. Restart the worker; recovery makes the state safe first.
    db.recover_interrupted_jobs()
    after = db.get_job(job_id)
    assert after.state_enum() in {
        JobState.QUEUED, JobState.RETRY_WAIT, JobState.NEEDS_RECONCILIATION,
        JobState.FAILED, JobState.UPLOADING, JobState.PUBLISHING,
    }, f"recovery left it in {after.state_enum()}"
    # The claim is released by recovery, so the job can be picked up again.
    reclaimed = db.claim_next_job("worker-1")
    assert reclaimed is not None and reclaimed["id"] == job_id, (
        "recovery left the job unclaimable")

    # 6. Continuation.  NEEDS_RECONCILIATION deliberately does not re-run on
    # its own - the pool must not guess whether Spotify already has the
    # episode - so the user (or the dashboard's Retry now) is what releases it.
    from app.core.queue import JobWorker

    service_retry = harness.service
    service_retry.db = db
    service_retry.retry_job(job_id, now=True)

    worker = JobWorker(harness.config, db, adapter=harness.adapter,
                       worker_id="worker-1", archive_manager=harness.worker.archive)
    for _ in range(6):
        if worker.run_once() is None:
            break

    final = db.get_job(job_id)
    assert final.state_enum() is JobState.ARCHIVED, final.state
    assert final["publish_confirmed_at"], "the continuation never confirmed"
    assert len(harness.spotify.episodes()) == 1, "the recovery published twice"


def test_a_leased_job_is_never_permanently_locked(harness: Harness):
    """A claim from a worker that died must not block the job forever."""
    harness.drop_pair("LOCK", kind="mp3")
    job_id = harness.discover()[0]["id"]

    # Simulate a worker that claimed the job and never came back.
    claimed = harness.db.claim_next_job("worker-dead")
    assert claimed is not None and claimed["id"] == job_id

    # A live claim genuinely blocks a second worker - that is the point.
    assert harness.db.claim_next_job("worker-2") is None, (
        "two workers were handed the same job")

    # Recovery at startup is what releases it.
    harness.db.recover_interrupted_jobs()
    again = harness.db.claim_next_job("worker-2")
    assert again is not None, "the job is permanently locked after recovery"
    assert again["id"] == job_id
    assert again["claimed_by"] == "worker-2"


def test_two_recoveries_in_a_row_are_idempotent(harness: Harness):
    """Recovery must be safe to run again on every start."""
    harness.drop_pair("REC", kind="mp3")
    jobs = harness.discover()
    harness.worker.adapter.publish = lambda *a, **k: (_ for _ in ()).throw(
        SystemExit("power cut"))
    with pytest.raises(SystemExit):
        harness.worker.run_once()

    first = harness.job(jobs[0]["id"])
    harness.db.recover_interrupted_jobs()
    second = harness.job(jobs[0]["id"])
    harness.db.recover_interrupted_jobs()
    third = harness.job(jobs[0]["id"])

    assert second["state"] == third["state"], "recovery is not idempotent"
    assert first["id"] == third["id"]
    assert len(harness.spotify.episodes()) == 0


def test_the_database_is_usable_after_an_abrupt_connection_close(harness: Harness):
    """An open cursor and an unfinished transaction must not corrupt the file."""
    harness.drop_pair("WAL", kind="mp3")
    job_id = harness.discover()[0]["id"]

    # Leave a read transaction open and abandon it, then reopen.
    connection = sqlite3.connect(harness.db.path)
    connection.execute("BEGIN")
    connection.execute("SELECT count(*) FROM jobs").fetchone()
    del connection
    gc.collect()

    db = _reopen(harness)
    assert db.get_job(job_id) is not None
    assert db.list_jobs(limit=10), "the reopened database lost its jobs"
    # And the whole workflow still finishes from there.
    harness.db = db
    harness.drain()
    assert harness.job(job_id).state_enum() is JobState.ARCHIVED


def test_no_job_is_left_in_a_mid_flight_state_after_recovery(harness: Harness):
    """After recovery, nothing may be stuck in a state no worker owns.

    This is also the regression guard for a real defect the release audit
    found.  Two different recordings shared a title and both were interrupted
    mid-upload; the second one reconciled against the episode the *first* had
    published, adopted it, and was archived as though it had been published.
    The second recording disappeared without a trace.

    A remote episode already recorded against a different local job is never
    this file's episode, however well the titles line up.  Disabling that
    check makes this test fail with one episode published instead of two.
    """
    for base in ("M1", "M2"):
        harness.drop_pair(base, kind="mp3")
    jobs = harness.discover()
    assert len(jobs) == 2

    def crash(*args, **kwargs):
        raise SystemExit("power cut")

    original = harness.worker.adapter.publish
    harness.worker.adapter.publish = crash
    for _ in range(2):
        try:
            harness.worker.run_once()
        except SystemExit:
            pass

    harness.db.recover_interrupted_jobs()
    # The power is back on; the adapter works again.
    harness.worker.adapter.publish = original

    mid_flight = {JobState.UPLOADING, JobState.PROCESSING,
                  JobState.READY_TO_PUBLISH, JobState.PUBLISHING,
                  JobState.ARCHIVING}
    for job in harness.db.list_jobs(limit=50):
        assert job.state_enum() not in mid_flight, (
            f"{job['id']} is stuck in {job.state_enum()} after recovery")

    # A job that had already sent something comes back as
    # NEEDS_RECONCILIATION and waits for the user, which is the whole point -
    # so release them the way Retry now does, then let them finish.
    for job in harness.db.list_jobs(limit=50):
        if job.state_enum() is JobState.NEEDS_RECONCILIATION:
            harness.service.retry_job(job["id"], now=True)
    harness.drain()

    assert len(harness.spotify.episodes()) == 2, (
        "the recovered jobs did not both publish")
    for job_id in {j["id"] for j in jobs}:
        assert harness.job(job_id).state_enum() is JobState.ARCHIVED


def test_recovery_records_evidence_for_a_job_that_reached_spotify(harness: Harness):
    """A job that got bytes out must be distinguishable from a fresh one.

    This is the evidence the duplicate policy depends on, so it has to
    survive a crash: otherwise a restart would treat a half-sent episode as
    brand new and could publish it twice.
    """
    harness.drop_pair("EV", kind="mp3")
    job_id = harness.discover()[0]["id"]

    harness.worker.adapter.publish = lambda *a, **k: (_ for _ in ()).throw(
        SystemExit("power cut"))
    with pytest.raises(SystemExit):
        harness.worker.run_once()

    harness.db.recover_interrupted_jobs()
    job = harness.job(job_id)
    assert job["publish_attempted_at"] or job["upload_started_at"], (
        "recovery forgot that this job had already reached Spotify")
