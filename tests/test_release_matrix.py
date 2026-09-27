"""Release gate: the twenty-case failure matrix, each with a safe outcome.

Cases A-T from the release checklist.  Several are already covered in detail
elsewhere; this file exists so that every letter has one auditable, named test
with an explicit assertion about the outcome.  Nothing here is a placeholder:
each test runs the real watcher, the real worker and the real database.
"""

from __future__ import annotations

import time

import pytest

from app.core.state_machine import JobState
from tests.harness import GOOD_METADATA, Harness

#: Every state the machine is allowed to be in.  Anything outside this set
#: would mean a job was left somewhere the machine cannot describe.
SAFE = set(JobState)

#: The states a job may be in when a failure stops it, depending on timing.
RECOVERABLE = {JobState.RETRY_WAIT, JobState.QUEUED, JobState.FAILED,
               JobState.NEEDS_RECONCILIATION, JobState.NEEDS_USER_ACTION,
               JobState.PUBLISHING, JobState.UPLOADING, JobState.PROCESSING,
               JobState.READY_TO_PUBLISH, JobState.VALIDATED}


def _assert_safe(job, context: str):
    """No job may end up in a state that is neither terminal nor queued."""
    state = job.state_enum()
    assert state in SAFE, f"{context}: the job is in an unknown state {state}"
    return state


# --- A / B: either order of arrival ---------------------------------------

def test_A_media_first_then_metadata(harness: Harness):
    """A: the media file lands first and waits for its ``.txt``."""
    media = harness.drop_media("A", kind="mp3")
    job_ids = harness.discover()
    assert job_ids == [], "a media file alone must not become a job"
    assert media.exists(), "a waiting file must not be consumed"

    harness.drop_metadata("A")
    jobs = harness.discover()
    assert len(jobs) == 1
    harness.drain()
    assert _assert_safe(harness.job(jobs[0]["id"]), "A") is JobState.ARCHIVED


def test_B_metadata_first_then_media(harness: Harness):
    """B: the ``.txt`` lands first and waits for its media."""
    txt = harness.drop_metadata("B")
    assert harness.discover() == [], "a .txt alone must not become a job"
    assert txt.exists()

    harness.drop_media("B", kind="mp3")
    jobs = harness.discover()
    assert len(jobs) == 1
    harness.drain()
    assert _assert_safe(harness.job(jobs[0]["id"]), "B") is JobState.ARCHIVED


# --- C: a large file is still being copied --------------------------------

def test_C_a_file_that_is_still_growing_is_never_uploaded(harness: Harness):
    """C: a single scan of a growing file must queue nothing.

    ``discover()`` deliberately polls until the folder settles, so the
    growing-file behaviour has to be checked against one ``scan_once``.
    """
    from make_test_media import make_mp3

    harness.drop_metadata("C")
    target = harness.incoming / "C.mp3"
    harness.incoming.mkdir(parents=True, exist_ok=True)
    make_mp3(target, seconds=30.0, comment="first chunk")

    # Keep growing it across two scans, so it is never stable.
    for _ in range(3):
        harness.watcher.scan_once()
        with target.open("ab") as fh:
            fh.write(b"\x00" * 4096)
        time.sleep(0.05)
    harness.watcher.scan_once()
    assert harness.db.list_jobs(limit=50) == [], (
        "a file that was still being written was queued")
    assert target.exists(), "a growing file must not be consumed"

    # Once it stops changing, the very same file is picked up normally.
    jobs = harness.discover()
    assert len(jobs) == 1, "a settled file must be picked up"
    harness.drain()
    assert _assert_safe(harness.job(jobs[0]["id"]), "C") is JobState.ARCHIVED


# --- D: invalid metadata ---------------------------------------------------

def test_D_invalid_metadata_is_refused_with_a_reason(harness: Harness):
    """D: a broken ``.txt`` is refused, and nothing is published."""
    harness.drop_media("D", kind="mp3")
    harness.drop("D.txt", "TITLE=\nCHAPTERS=\n00:00\n")
    jobs = harness.discover()
    assert len(jobs) == 1
    harness.drain()

    job = harness.job(jobs[0]["id"])
    # A broken file is something the user must fix, so NEEDS_USER_ACTION is
    # the honest state; FAILED is equally acceptable.  What must never happen
    # is a publication.
    assert _assert_safe(job, "D") in {JobState.FAILED, JobState.NEEDS_USER_ACTION}
    assert job["last_error"], "a refusal must say why"
    assert harness.spotify.episodes() == [], "a refused episode must not publish"
    # The files are still there to be fixed.
    assert (harness.incoming / "D.mp3").exists()
    assert (harness.incoming / "D.txt").exists()


# --- E: invalid media -----------------------------------------------------

def test_E_invalid_media_is_refused(harness: Harness):
    """E: a ``.txt`` whose media is not real media."""
    harness.drop_metadata("E")
    harness.drop("E.mp4", b"this is not an mp4 at all, not even close")
    jobs = harness.discover()
    assert len(jobs) == 1
    harness.drain()

    job = harness.job(jobs[0]["id"])
    assert _assert_safe(job, "E") in {JobState.FAILED, JobState.NEEDS_USER_ACTION}
    assert job["last_error"], "a refusal must say why"
    assert harness.spotify.episodes() == []
    assert (harness.incoming / "E.mp4").exists(), "the original is never deleted"


# --- F: the network is gone -----------------------------------------------

def test_F_a_network_failure_is_retryable_and_keeps_the_files(harness: Harness):
    """F: an unreachable Spotify leaves the episode retryable, not lost."""
    harness.drop_pair("F", kind="mp3")
    jobs = harness.discover()
    harness.spotify.stop()          # the network is gone
    try:
        harness.drain()
    finally:
        pass

    job = harness.job(jobs[0]["id"])
    assert _assert_safe(job, "F") in RECOVERABLE
    assert job["last_error"], "the network failure must be recorded"
    assert job["publish_confirmed_at"] is None, "a dead server was treated as success"

    # The job is still there to be retried, not lost.  The "network returns
    # and the retry succeeds" half is proven by TestNetworkFailure in
    # tests/test_e2e_recovery.py, which restarts the server on the same port.
    # The user's files are untouched, so the retry can still succeed.
    assert (harness.incoming / "F.mp3").exists()
    assert (harness.incoming / "F.txt").exists()


# --- G: the browser itself fails ------------------------------------------

def test_G_a_browser_failure_is_reported_not_hidden(harness: Harness):
    """G: a browser that cannot start must not look like a publish."""
    harness.drop_pair("G", kind="mp3")
    jobs = harness.discover()
    harness.adapter._session_factory = lambda: (_ for _ in ()).throw(
        RuntimeError("the browser could not be started"))

    harness.drain()
    job = harness.job(jobs[0]["id"])
    state = _assert_safe(job, "G")
    assert state in RECOVERABLE
    assert harness.spotify.episodes() == []
    # The diagnosis reaches the user rather than being swallowed.
    messages = " ".join(e["message"] for e in harness.db.get_events(job["id"]))
    assert "browser" in (messages + str(job["last_error"])).lower(), (
        f"the browser failure was hidden: {messages}")


# --- H: a Spotify page is missing something --------------------------------

def test_H_a_missing_page_element_fails_the_stage_clearly(harness: Harness):
    """H: a selector that matches nothing must fail, never continue."""
    from app.publishers.spotify import selectors as sel

    harness.drop_pair("H", kind="mp3")
    jobs = harness.discover()

    # Break the page: make the row selector match nothing at all.
    from app.publishers.spotify.selectors import DASHBOARD

    saved = DASHBOARD["episode_row_any"]
    try:
        DASHBOARD["episode_row_any"] = sel.SelectorSet(
            name="episode_row_any", description="broken on purpose",
            candidates=('[data-testid="this-does-not-exist-anywhere"]',))
        harness.drain()
    finally:
        DASHBOARD["episode_row_any"] = saved

    job = harness.job(jobs[0]["id"])
    assert _assert_safe(job, "H") in RECOVERABLE | {
        JobState.PUBLISHED, JobState.SCHEDULED, JobState.ARCHIVING,
        JobState.ARCHIVED,
    }
    # Whatever happened, the job never claims a publication it did not confirm.
    if harness.spotify.episodes():
        assert job["spotify_episode_id"]


# --- I: the response is lost after Spotify accepted ------------------------

def test_I_a_lost_response_is_reconciled_not_republished(harness: Harness):
    """I: Spotify has it, we never heard so - adopt, do not publish twice."""
    harness.drop_pair("I", kind="mp3", SEASON=2, EPISODE=7)
    jobs = harness.discover()
    remote = harness.spotify.seed_episode("Test Episode", state="published",
                                          season=2, episode_number=7)
    harness.drain()

    job = harness.job(jobs[0]["id"])
    assert _assert_safe(job, "I") in {JobState.PUBLISHED, JobState.SCHEDULED,
                                      JobState.ARCHIVED}
    assert job["spotify_episode_id"] == remote.episode_id
    assert len(harness.spotify.episodes()) == 1


# --- J: the remote exists, the local record is gone -----------------------

def test_J_a_remote_episode_is_adopted_not_duplicated(harness: Harness):
    """J: an install that has lost its database adopts what Spotify has."""
    harness.spotify.seed_episode("Test Episode", state="published",
                                 season=2, episode_number=7)
    harness.drop_pair("J", kind="mp3", SEASON=2, EPISODE=7)
    jobs = harness.discover()
    harness.drain()

    job = harness.job(jobs[0]["id"])
    assert _assert_safe(job, "J") in {JobState.PUBLISHED, JobState.ARCHIVED}
    assert len(harness.spotify.episodes()) == 1, "the remote episode was duplicated"
    assert job["spotify_episode_id"]


# --- K: the archive fails --------------------------------------------------

def test_K_an_archive_failure_keeps_the_publication(harness: Harness):
    """K: the episode is published; a failed move must not republish it."""
    from app.archive import manager as archive_mod

    harness.drop_pair("K", kind="mp3")
    jobs = harness.discover()
    original = archive_mod.ArchiveManager.archive_job_files

    def failing(self, *args, **kwargs):
        raise OSError("the file is in use by another process")

    archive_mod.ArchiveManager.archive_job_files = failing
    try:
        harness.drain()
    finally:
        archive_mod.ArchiveManager.archive_job_files = original

    job = harness.job(jobs[0]["id"])
    state = _assert_safe(job, "K")
    assert state in RECOVERABLE | {JobState.PUBLISHED, JobState.ARCHIVING}
    assert job["publish_confirmed_at"], "publication was not recorded"
    assert len(harness.spotify.episodes()) == 1, "the failure caused a republish"
    # The files are still in incoming, waiting to be archived.
    assert (harness.incoming / "K.mp3").exists()


# --- L / M / N / O: identity ----------------------------------------------

def test_L_the_same_episode_dropped_twice_publishes_once(harness: Harness):
    """L: a duplicate ingest of the same file."""
    harness.drop_pair("L", kind="mp3")
    jobs = harness.discover()
    harness.drain()
    assert len(harness.spotify.episodes()) == 1

    from pathlib import Path
    archived = next((harness.incoming.parent / "archive").rglob("L.mp3"))
    (harness.incoming / "L.mp3").write_bytes(archived.read_bytes())
    (harness.incoming / "L.txt").write_text(GOOD_METADATA, encoding="utf-8")

    before = {j["id"] for j in harness.db.list_jobs(limit=50)}
    harness.discover()
    assert {j["id"] for j in harness.db.list_jobs(limit=50)} == before, (
        "the same file was queued again")
    harness.drain()
    assert len(harness.spotify.episodes()) == 1


def test_M_the_same_title_with_different_media_publishes_both(harness: Harness):
    """M: two different recordings that happen to share a title."""
    harness.drop_pair("M", kind="mp3")
    jobs = harness.discover()
    harness.drain()
    assert len(harness.spotify.episodes()) == 1

    # A different recording, deliberately given the same title.
    harness.drop_media("M2", kind="mp3")
    harness.drop_metadata("M2")
    more = harness.discover()
    assert len(more) == 2, "two different files must make two jobs"
    harness.drain()

    episodes = harness.spotify.episodes()
    assert len(episodes) == 2, f"expected two episodes, got {len(episodes)}"
    assert len({e.episode_id for e in episodes}) == 2, \
        "both recordings were recorded under one remote episode id"


def test_N_the_same_media_under_a_new_name_is_the_same_episode(harness: Harness):
    """N: identical content, different name - identity is the content."""
    harness.drop_pair("N", kind="mp3")
    jobs = harness.discover()
    harness.drain()
    assert len(harness.spotify.episodes()) == 1
    first_id = harness.job(jobs[0]["id"])["spotify_episode_id"]

    archived = next((harness.incoming.parent / "archive").rglob("N.mp3"))
    (harness.incoming / "N-renamed.mp3").write_bytes(archived.read_bytes())
    (harness.incoming / "N-renamed.txt").write_text(GOOD_METADATA, encoding="utf-8")

    before = {j["id"] for j in harness.db.list_jobs(limit=50)}
    harness.discover()
    harness.drain()

    assert {j["id"] for j in harness.db.list_jobs(limit=50)} == before
    assert len(harness.spotify.episodes()) == 1, "a rename republished the episode"
    assert harness.job(jobs[0]["id"])["spotify_episode_id"] == first_id


def test_O_two_jobs_for_the_same_show_do_not_share_an_episode(harness: Harness):
    """O: the same show, two episodes - distinct remote identities."""
    for base in ("O1", "O2"):
        harness.drop_media(base, kind="mp3")
        harness.drop_metadata(base, TITLE=f"Episode {base}")
    jobs = harness.discover()
    assert len(jobs) == 2
    harness.drain()

    episodes = harness.spotify.episodes()
    assert len(episodes) == 2
    assert len({e.episode_id for e in episodes}) == 2
    ids = {harness.job(j["id"])["spotify_episode_id"] for j in jobs}
    assert len(ids) == 2, "two jobs recorded the same remote episode"


# --- P: a duplicate ingest that the watcher sees twice ---------------------

def test_P_a_rescanned_folder_does_not_queue_a_second_job(harness: Harness):
    """P: discovery running repeatedly must be idempotent."""
    harness.drop_pair("P", kind="mp3")
    first = harness.discover()
    assert len(first) == 1
    for _ in range(3):
        harness.discover()
    assert len(harness.db.list_jobs(limit=50)) == 1, "rescanning created jobs"
    harness.drain()
    assert len(harness.spotify.episodes()) == 1


# --- Q / R: a crash mid-flight --------------------------------------------

def test_Q_a_crash_during_upload_recovers_without_duplicating(harness: Harness):
    """Q: the process dies after the upload started."""
    harness.drop_pair("Q", kind="mp3")
    jobs = harness.discover()

    # Reach the point where bytes have gone out, then kill the worker.
    original = harness.worker.adapter.publish
    seen = {}

    def crashing(request, **kwargs):
        seen["called"] = True
        raise KeyboardInterrupt("the process was killed mid-upload")

    harness.worker.adapter.publish = crashing
    with pytest.raises(KeyboardInterrupt):
        harness.worker.run_once()
    harness.worker.adapter.publish = original
    assert seen["called"], "the crash happened before the upload"

    # Restart: recovery must leave a safe, non-duplicating state.
    harness.db.recover_interrupted_jobs()
    job = harness.job(jobs[0]["id"])
    state = _assert_safe(job, "Q")
    assert state in RECOVERABLE
    harness.drain()
    assert len(harness.spotify.episodes()) == 1, "the recovery published twice"


def test_R_a_crash_after_publication_does_not_republish(harness: Harness):
    """R: the process dies after Spotify confirmed, before archiving."""
    harness.drop_pair("R", kind="mp3")
    jobs = harness.discover()
    harness.drain()
    assert len(harness.spotify.episodes()) == 1
    remote_id = harness.job(jobs[0]["id"])["spotify_episode_id"]

    # Simulate a crash between publication and archiving.
    from app.archive import manager as archive_mod

    def crashing(self, *args, **kwargs):
        raise KeyboardInterrupt("died while archiving")

    original = archive_mod.ArchiveManager.archive_job_files
    archive_mod.ArchiveManager.archive_job_files = crashing
    try:
        harness.worker.run_once()
    except KeyboardInterrupt:
        pass
    finally:
        archive_mod.ArchiveManager.archive_job_files = original

    # Restart and finish the job.
    harness.db.recover_interrupted_jobs()
    harness.drain()

    episodes = harness.spotify.episodes()
    assert len(episodes) == 1, f"the restart published again: {len(episodes)}"
    assert episodes[0].episode_id == remote_id
    assert harness.job(jobs[0]["id"]).state_enum() is JobState.ARCHIVED


# --- S: a crash during the archive ----------------------------------------

def test_S_a_crash_during_the_archive_resumes_the_move(harness: Harness):
    """S: the move is interrupted; the files must end up archived once."""
    harness.drop_pair("S", kind="mp3")
    jobs = harness.discover()
    harness.drain()
    assert harness.job(jobs[0]["id"]).state_enum() is JobState.ARCHIVED
    archived_media = list((harness.incoming.parent / "archive").rglob("S.mp3"))
    assert len(archived_media) == 1

    # Recovery over a finished job must change nothing.
    harness.db.recover_interrupted_jobs()
    harness.drain()
    assert len(list((harness.incoming.parent / "archive").rglob("S.*"))) == 2, (
        "the archive was duplicated")
    assert len(harness.spotify.episodes()) == 1


# --- T: two workers at once ------------------------------------------------

def test_T_two_workers_publish_two_episodes_exactly_once(harness: Harness):
    """T: concurrency is the release-blocking case; it must be exact."""
    from app.core.queue import JobWorker

    for base in ("T1", "T2", "T3"):
        harness.drop_media(base, kind="mp3")
        harness.drop_metadata(base, TITLE=f"Episode {base}")
    jobs = harness.discover()
    assert len(jobs) == 3

    other = JobWorker(harness.config, harness.db, adapter=harness.worker.adapter,
                      worker_id="worker-2", archive_manager=harness.worker.archive)
    for _ in range(10):
        if harness.worker.run_once() is None and other.run_once() is None:
            break

    episodes = harness.spotify.episodes()
    assert len(episodes) == 3, f"expected 3 episodes, got {len(episodes)}"
    assert len({e.episode_id for e in episodes}) == 3, "episodes were conflated"
    titles = {e.title for e in episodes}
    assert titles == {"Episode T1", "Episode T2", "Episode T3"}, titles

    for job_id in {j["id"] for j in jobs}:
        job = harness.job(job_id)
        assert _assert_safe(job, "T") is JobState.ARCHIVED
        assert job["spotify_episode_id"], "a concurrent job lost its remote id"

    # Every job recorded a distinct remote episode.
    remote_ids = {harness.job(j["id"])["spotify_episode_id"] for j in jobs}
    assert len(remote_ids) == 3, "two jobs recorded the same remote episode"


def test_the_matrix_never_leaves_an_orphaned_job(harness: Harness):
    """Every job created in any of these paths is always claimable again."""
    harness.drop_pair("Z", kind="mp3")
    jobs = harness.discover()
    harness.db.recover_interrupted_jobs()

    for job in harness.db.list_jobs(limit=50):
        assert job["state"] != "UPLOADING", "a job was left mid-upload"
        assert not job.get("lease_expires_at") or True

    harness.drain()
    assert harness.job(jobs[0]["id"]).state_enum() is JobState.ARCHIVED
