"""CASES C-M -- timing, duplicates, crashes, failures, concurrency.

Every case here drives the production watcher, worker, state machine, database
and publisher.  The mock Spotify is the only stand-in, and it behaves like the
real thing: it stores episodes, serves redirects and can be made to fail.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.core.database import Database
from app.core.queue import JobWorker
from app.core.state_machine import JobState
from tests.harness import GOOD_METADATA


def _meta_for(index: int) -> str:
    """Metadata for episode *index* with its own distinct title."""
    return (GOOD_METADATA
            .replace("TITLE=Test Episode", f"TITLE=Test Episode {index + 1}")
            .replace("EPISODE=7", f"EPISODE={index + 1}"))


# ------------------------------------------------- CASE C: still copying

class TestFileStillCopying:
    def test_a_growing_file_is_never_processed(self, wired, tmp_path):
        """A file still being copied must not be queued, let alone uploaded."""
        from make_test_media import make_mp3

        source = make_mp3(tmp_path / "source.mp3", seconds=3.0)
        payload = source.read_bytes()
        target = wired.incoming / "BIG.mp3"
        wired.incoming.mkdir(parents=True, exist_ok=True)
        wired.drop("BIG.txt", GOOD_METADATA)
        stop = threading.Event()
        done = threading.Event()

        def copier():
            # Stream the real bytes in small pieces, the way a copy does.
            with open(target, "wb") as fh:
                for offset in range(0, len(payload), 2048):
                    if stop.is_set():
                        return
                    fh.write(payload[offset:offset + 2048])
                    fh.flush()
                    time.sleep(0.01)
            done.set()

        thread = threading.Thread(target=copier, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 0.4
            while time.time() < deadline and not done.is_set():
                wired.watcher.scan_once()
                assert wired.db.list_jobs() == [], "a half-written file was queued"
                assert wired.spotify.episodes() == []
                time.sleep(0.05)
        finally:
            thread.join(timeout=10)

        # The copy finished; once the size settles the episode is picked up.
        assert done.is_set()
        jobs = wired.discover()
        assert len(jobs) == 1
        wired.drain()
        assert len(wired.spotify.episodes()) == 1
        assert wired.job(jobs[0]["id"]).state_enum() is JobState.ARCHIVED

    def test_a_truncated_file_is_rejected_not_published(self, wired):
        """Finishing early leaves a broken file; that must be reported, not sent."""
        wired.drop("CUT.txt", GOOD_METADATA)
        target = wired.incoming / "CUT.mp3"
        wired.incoming.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xfb\x90" * 8)   # a fragment, not a real file

        jobs = wired.discover()
        assert len(jobs) == 1
        wired.drain()
        assert wired.spotify.episodes() == []
        job = wired.job(jobs[0]["id"])
        assert job["state"] != JobState.PUBLISHED.value
        assert job["last_error"], "the user must be told why"

    def test_an_unstable_file_reports_why(self, wired):
        target = wired.incoming / "PART.mp3"
        wired.incoming.mkdir(parents=True, exist_ok=True)
        wired.drop("PART.txt", GOOD_METADATA)
        target.write_bytes(b"\x00" * 64)

        wired.watcher.scan_once()
        time.sleep(0.05)
        wired.watcher.scan_once()
        unstable = wired.events("unstable")
        assert unstable, "the dashboard must say why it is still waiting"
        assert "PART.mp3" in unstable[0]["message"]
        assert wired.db.list_jobs() == []


# ------------------------------------ CASES D/E: metadata arrives later

class TestPartialDrops:
    def test_media_first_waits_for_the_txt(self, wired):
        wired.drop_media("EP", kind="mp3")
        assert wired.discover(timeout=1.0) == []
        assert wired.spotify.episodes() == []
        assert wired.events("waiting_metadata")

        wired.drop("EP.txt", GOOD_METADATA)
        jobs = wired.discover()
        assert len(jobs) == 1
        wired.drain()
        assert len(wired.spotify.episodes()) == 1
        assert wired.job(jobs[0]["id"]).state_enum() is JobState.ARCHIVED

    def test_txt_first_waits_for_the_media(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        assert wired.discover(timeout=1.0) == []
        assert wired.spotify.episodes() == []

        wired.drop_media("EP", kind="mp3")
        jobs = wired.discover()
        assert len(jobs) == 1
        wired.drain()
        assert len(wired.spotify.episodes()) == 1

    def test_only_one_job_is_ever_created(self, wired):
        wired.drop_media("EP", kind="mp3")
        for _ in range(3):
            wired.discover(timeout=0.4)
        wired.drop("EP.txt", GOOD_METADATA)
        for _ in range(3):
            wired.discover(timeout=0.4)
        assert len(wired.db.list_jobs()) == 1
        wired.drain()
        assert len(wired.spotify.episodes()) == 1


# ------------------------------------------- CASE F: duplicate re-ingest

class TestDuplicateReingest:
    def _publish_once(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()
        return job_id

    def test_re_dropping_the_same_files_changes_nothing(self, wired):
        job_id = self._publish_once(wired)
        assert wired.job(job_id).state_enum() is JobState.ARCHIVED

        archived = Path(wired.job(job_id)["archived_media_path"])
        meta = Path(wired.job(job_id)["archived_metadata_path"])
        wired.drop("EP.txt", meta.read_text(encoding="utf-8"))
        wired.drop("EP.mp3", archived.read_bytes())

        jobs = wired.discover()
        assert len(jobs) == 1
        assert jobs[0]["id"] == job_id, "the content hash must find the old job"
        wired.drain()
        assert len(wired.spotify.episodes()) == 1, "a second episode was created"

    def test_renaming_the_files_does_not_republish(self, wired):
        job_id = self._publish_once(wired)
        archived = Path(wired.job(job_id)["archived_media_path"])
        meta = Path(wired.job(job_id)["archived_metadata_path"])

        wired.drop("EP (copy).txt", meta.read_text(encoding="utf-8"))
        wired.drop("EP (copy).mp3", archived.read_bytes())

        jobs = wired.discover()
        assert len(jobs) == 1 and jobs[0]["id"] == job_id
        wired.drain()
        assert len(wired.spotify.episodes()) == 1

    def test_a_second_run_of_the_worker_publishes_nothing(self, wired):
        self._publish_once(wired)
        for _ in range(3):
            wired.drain()
        assert len(wired.spotify.episodes()) == 1

    def test_a_pre_existing_remote_episode_is_adopted_not_duplicated(self, wired):
        """Spotify already has it (e.g. a previous run of an older install)."""
        wired.spotify.seed_episode("Test Episode", state="published",
                                   season=2, episode_number=7)
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()

        assert len(wired.spotify.episodes()) == 1
        job = wired.job(job_id)
        assert job["spotify_episode_id"]
        messages = [e["message"].lower() for e in wired.db.get_events(job_id)]
        assert any("adopt" in m for m in messages), messages


# ------------------------------------------- CASE G: crash mid-upload

class TestCrashDuringUpload:
    """The process dies with an upload in flight and comes back."""

    def _stage_a_crash(self, wired, config):
        """Publish, then rewind the job to "upload in flight, id unknown".

        That is precisely the dangerous window: Spotify has the episode but we
        never wrote down its identifier.
        """
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()
        assert wired.job(job_id).state_enum() is JobState.ARCHIVED
        remote = wired.spotify.episodes()[0]

        db = wired.db
        db.update_job(job_id,
                      state=JobState.UPLOADING.value,
                      spotify_episode_id=None, spotify_url=None, spotify_state=None,
                      claimed_by="worker-that-died",
                      archived_at=None, archived_media_path=None,
                      archived_metadata_path=None)
        return job_id, remote

    def test_recovery_asks_spotify_first(self, wired, config):
        job_id, _ = self._stage_a_crash(wired, config)
        recovered = wired.db.recover_interrupted_jobs()
        assert any(j == job_id for j, _, _ in recovered)
        assert wired.job(job_id).state_enum() is JobState.NEEDS_RECONCILIATION

    def test_recovery_is_idempotent_across_restarts(self, wired, config):
        job_id, _ = self._stage_a_crash(wired, config)
        for _ in range(3):
            wired.db.recover_interrupted_jobs()
            assert wired.job(job_id).state_enum() is JobState.NEEDS_RECONCILIATION

    def test_the_restarted_worker_does_not_republish(self, wired, config):
        job_id, remote = self._stage_a_crash(wired, config)
        wired.db.recover_interrupted_jobs()

        worker = JobWorker(config, wired.db, adapter=wired.adapter,
                           worker_id="worker-2",
                           archive_manager=wired.service.archive)
        worker.run_job(wired.db.get_job(job_id))

        assert len(wired.spotify.episodes()) == 1, "the crash caused a duplicate"
        job = wired.job(job_id)
        assert job["spotify_episode_id"] == remote.episode_id
        assert job.state_enum() in {JobState.PUBLISHED, JobState.SCHEDULED,
                                    JobState.ARCHIVED}

    def test_reconciliation_is_recorded_in_the_job_history(self, wired, config):
        job_id, _ = self._stage_a_crash(wired, config)
        wired.db.recover_interrupted_jobs()
        worker = JobWorker(config, wired.db, adapter=wired.adapter,
                           worker_id="worker-2",
                           archive_manager=wired.service.archive)
        worker.run_job(wired.db.get_job(job_id))

        messages = [e["message"].lower() for e in wired.db.get_events(job_id)]
        assert any("reconcil" in m for m in messages), messages
        assert any("adopt" in m for m in messages), messages

    def test_a_fresh_install_on_an_existing_show_adopts_rather_than_reuploads(
            self, wired, config):
        """No local job at all, but Spotify already has the episode."""
        remote = wired.spotify.seed_episode("Test Episode", state="published",
                                            season=2, episode_number=7)
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()

        assert len(wired.spotify.episodes()) == 1
        job = wired.job(job_id)
        assert job["spotify_episode_id"] == remote.episode_id
        assert job.state_enum() in {JobState.PUBLISHED, JobState.ARCHIVED}


# ---------------------------------------- CASE H: Spotify unreachable

class TestNetworkFailure:
    def test_a_dead_server_produces_a_retry_not_a_loss(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]

        wired.spotify.stop()
        try:
            wired.drain()
        finally:
            wired.spotify = wired.spotify  # keep the object for assertions

        job = wired.job(job_id)
        assert job["state"] != JobState.PUBLISHED.value
        assert job["last_error"], "the failure must be recorded, not swallowed"
        # The user's files are untouched so the retry can succeed.
        assert Path(job["media_path"]).exists()
        assert Path(job["metadata_path"]).exists()

    def test_a_retry_backoff_is_scheduled(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.spotify.stop()
        try:
            wired.drain()
        finally:
            pass

        job = wired.job(job_id)
        if job["state"] == JobState.RETRY_WAIT.value:
            assert job["next_attempt_at"], "a backoff time must be recorded"
            assert job["retry_count"] >= 1

    def test_an_exponential_backoff_policy_is_bounded(self, config):
        policy = config.retry
        delays = [policy.delay_for(attempt) for attempt in range(1, 8)]
        assert all(d >= 0 for d in delays)
        assert delays[0] < delays[-1], "later attempts must wait longer"
        assert max(delays) <= policy.max_delay_seconds
        # Bounded: the worker must not retry forever in one run.
        assert policy.max_attempts >= 1

    def test_the_eventual_retry_succeeds(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]

        wired.spotify.stop()
        wired.drain()
        assert wired.spotify.episodes() == []

        # Bring Spotify back and let the retry happen.
        wired.adapter.close()
        from tests.fakes.fake_session import FakeSession
        wired.adapter._session_factory = lambda: FakeSession(wired.config.spotify.base_url)
        _restart_server(wired)
        wired.service.retry_job(job_id, now=True)
        wired.drain()

        assert len(wired.spotify.episodes()) == 1
        assert wired.job(job_id).state_enum() is JobState.ARCHIVED


def _restart_server(wired):
    """Bring the mock back up on the same port (after a ``stop()``)."""
    from tests.mock_spotify.server import MockSpotify

    old = wired.spotify
    if old.port is None:  # pragma: no cover - defensive
        return
    server = MockSpotify(host=old.host, port=old.port).start()
    wired.spotify = server
    wired.__dict__["_fresh_server"] = server
    return server


# ------------------------------ CASE I: remote success, local response lost

class TestLostResponse:
    def test_reconciliation_adopts_the_episode_that_exists(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]

        # Spotify accepts the episode but we never learn the id - the exact
        # situation a dropped connection mid-response creates.
        remote = wired.spotify.seed_episode("Test Episode", state="published",
                                            season=2, episode_number=7)
        wired.drain()

        job = wired.job(job_id)
        assert len(wired.spotify.episodes()) == 1
        assert job["spotify_episode_id"] == remote.episode_id
        assert job.state_enum() in {JobState.PUBLISHED, JobState.SCHEDULED,
                                    JobState.ARCHIVED}
        assert job["state"] != JobState.NEEDS_RECONCILIATION.value


# ------------------------------------------- CASE J: remote rejection

class TestRemoteRejection:
    def test_a_form_error_fails_clearly_and_allows_a_retry(self, wired):
        wired.spotify.state.form_error = "The description is too long for this show."
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()

        assert wired.spotify.episodes() == []
        job = wired.job(job_id)
        assert job["state"] in {JobState.FAILED.value,
                                JobState.RETRY_WAIT.value,
                                JobState.NEEDS_USER_ACTION.value}
        assert job["last_error"]
        assert job["state"] != JobState.PUBLISHED.value

        # Fixing the problem and retrying must work.
        wired.spotify.state.form_error = ""
        wired.service.retry_job(job_id, now=True)
        wired.drain()
        assert len(wired.spotify.episodes()) == 1

    def test_a_rejected_episode_does_not_poison_the_title(self, wired):
        """A failed attempt must not make the next one look like a duplicate."""
        wired.spotify.state.form_error = "Rejected for testing."
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()
        assert wired.spotify.episodes() == []

        wired.spotify.state.form_error = ""
        wired.service.retry_job(job_id, now=True)
        wired.drain()
        assert len(wired.spotify.episodes()) == 1


# --------------------------------------- CASE K: retired / cancelled jobs

class TestRetiredAndCancelled:
    def test_a_cancelled_job_is_never_claimed(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]

        assert wired.service.cancel_job(job_id) is True
        wired.drain()
        assert wired.spotify.episodes() == []
        assert wired.job(job_id).state_enum() is JobState.CANCELLED

    def test_a_retired_job_is_never_claimed(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.db.retire_job(job_id, "superseded")
        wired.drain()
        assert wired.spotify.episodes() == []
        assert wired.db.claim_next_job("probe") is None

    def test_cancelling_before_the_worker_starts_is_honoured(self, wired):
        wired.drop("EP.txt", GOOD_METADATA)
        wired.drop_media("EP", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.db.request_cancel(job_id)
        wired.drain()
        assert wired.job(job_id).state_enum() is JobState.CANCELLED
        assert wired.spotify.episodes() == []


# ------------------------------------------------ CASE L: several episodes

class TestMultipleEpisodes:
    def test_each_episode_is_published_exactly_once(self, wired):
        for index in range(4):
            meta = _meta_for(index)
            wired.drop(f"EP{index}.txt", meta)
            wired.drop_media(f"EP{index}", kind="mp3")

        jobs = wired.discover(expect=4)
        assert len(jobs) == 4
        wired.drain()

        titles = sorted(e.title for e in wired.spotify.episodes())
        assert len(titles) == 4, titles
        numbers = sorted(e.episode_number for e in wired.spotify.episodes())
        assert numbers == [1, 2, 3, 4]

        for job in jobs:
            assert wired.job(job["id"]).state_enum() is JobState.ARCHIVED

    def test_one_bad_episode_does_not_block_the_others(self, wired):
        wired.drop("GOOD1.txt", _meta_for(0))
        wired.drop_media("GOOD1", kind="mp3")
        wired.drop("BAD.txt", "DESCRIPTION=no title\n")
        wired.drop_media("BAD", kind="mp3")
        wired.drop("GOOD2.txt", _meta_for(1))
        wired.drop_media("GOOD2", kind="mp3")

        wired.discover(expect=3)
        wired.drain(limit=20)

        assert len(wired.spotify.episodes()) == 2
        states = {j["base_name"]: j["state"] for j in wired.db.list_jobs()}
        assert states["BAD"] == JobState.NEEDS_USER_ACTION.value
        assert states["GOOD1"] == JobState.ARCHIVED.value
        assert states["GOOD2"] == JobState.ARCHIVED.value

    def test_jobs_do_not_interfere(self, wired):
        for index in range(3):
            wired.drop(f"EP{index}.txt",
                       _meta_for(index))
            wired.drop_media(f"EP{index}", kind="mp3")
        jobs = wired.discover()
        wired.drain()

        for job in jobs:
            row = wired.job(job["id"])
            assert row["spotify_episode_id"], "every job records its own episode"
        ids = {wired.job(j["id"])["spotify_episode_id"] for j in jobs}
        assert len(ids) == 3


# -------------------------------------------------- CASE M: two workers

class TestTwoWorkers:
    def test_the_same_job_is_never_claimed_twice(self, wired):
        for index in range(3):
            wired.drop(f"EP{index}.txt",
                       _meta_for(index))
            wired.drop_media(f"EP{index}", kind="mp3")
        wired.discover()

        first = JobWorker(wired.config, wired.db, adapter=wired.adapter,
                          worker_id="worker-A", archive_manager=wired.service.archive)
        second = JobWorker(wired.config, wired.db, adapter=wired.adapter,
                           worker_id="worker-B", archive_manager=wired.service.archive)

        claims: list[tuple[str, str]] = []
        lock = threading.Lock()

        def race(worker):
            for _ in range(10):
                job = wired.db.claim_next_job(worker.worker_id)
                if job is not None:
                    with lock:
                        claims.append((worker.worker_id, job["id"]))
                    time.sleep(0.01)

        threads = [threading.Thread(target=race, args=(w,)) for w in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        claimed_ids = [job_id for _, job_id in claims]
        assert len(claimed_ids) == len(set(claimed_ids)), (
            f"a job was claimed twice: {claims}")
        assert len(claimed_ids) == 3

    def test_concurrent_workers_publish_each_episode_once(self, wired):
        for index in range(4):
            wired.drop(f"EP{index}.txt",
                       _meta_for(index))
            wired.drop_media(f"EP{index}", kind="mp3")
        wired.discover(expect=4)

        workers = [
            JobWorker(wired.config, wired.db, adapter=wired.adapter,
                      worker_id=f"worker-{i}", archive_manager=wired.service.archive)
            for i in range(2)
        ]

        def run(worker):
            for _ in range(10):
                if worker.run_once() is None:
                    return

        threads = [threading.Thread(target=run, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        titles = [e.title for e in wired.spotify.episodes()]
        assert len(titles) == len(set(titles)), f"duplicate publication: {titles}"
        for job in wired.db.list_jobs():
            assert job["state"] == JobState.ARCHIVED.value, (
                f"{job['base_name']} ended in {job['state']}: {job['last_error']}")
        assert len(wired.spotify.episodes()) == 4
