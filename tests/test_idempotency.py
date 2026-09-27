"""Duplicate-prevention audit.

Eleven identity scenarios, each named and asserted individually. The rule
underneath all of them:

    the same bytes are the same episode, and the same episode is published
    exactly once - whatever the file is called, whatever the local database
    says, and whatever happened the last time we tried.

Every test here drives the real watcher, the real database, the real worker
and the real publisher. Only the Spotify website is substituted.
"""
from __future__ import annotations

import pytest

from app.core.database import Database, JobState
from app.core.queue import JobWorker
from make_test_media import make_mp3


def _worker(wired) -> JobWorker:
    return JobWorker(wired.config, wired.db, adapter=wired.adapter)


def _titles(wired) -> list[str]:
    return sorted(e.title for e in wired.spotify.episodes())


def _pair(wired, name: str, **kwargs):
    """Drop a pair whose TITLE matches the file name.

    The harness writes a fixed placeholder title, so every test that asserts
    on a published title has to set it explicitly.
    """
    kwargs.setdefault("title", name)
    return wired.drop_pair(name, **kwargs)


# ---------------------------------------------------------------------------
# A. local job exists + remote episode exists
# ---------------------------------------------------------------------------
class TestA_LocalAndRemoteBothExist:
    def test_the_remote_episode_is_adopted_not_created_again(self, wired):
        _pair(wired, "Existing Episode")
        job = wired.discover()[0]
        # Spotify already has it - the state we reach after a lost response.
        remote = wired.spotify.seed_episode("Existing Episode", state="published")
        wired.force_state(job.id, JobState.NEEDS_RECONCILIATION, message="unsure")

        wired.drain()

        row = wired.db.get_job(job.id)
        assert row.state_enum() == JobState.ARCHIVED, row.state
        assert _titles(wired) == ["Existing Episode"], "a second episode was created"
        assert remote.episode_id in (row.spotify_episode_id or ""), (
            f"the remote id {remote.episode_id} was not adopted: "
            f"{row.spotify_episode_id}"
        )
        assert row.spotify_state, "the remote state must be recorded"

    def test_adoption_is_recorded_in_the_history(self, wired):
        _pair(wired, "Recorded Adoption")
        job = wired.discover()[0]
        wired.spotify.seed_episode("Recorded Adoption", state="published")
        wired.force_state(job.id, JobState.NEEDS_RECONCILIATION)
        wired.drain()

        messages = " ".join(e["message"] for e in wired.db.get_events(job.id))
        assert "adopt" in messages.lower() or "reconcil" in messages.lower(), messages


# ---------------------------------------------------------------------------
# B. local job exists + remote episode does not exist
# ---------------------------------------------------------------------------
class TestB_LocalOnly:
    def test_it_is_published_exactly_once(self, wired):
        _pair(wired, "Brand New")
        job = wired.discover()[0]
        assert wired.spotify.episodes() == []

        wired.drain()

        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED
        assert _titles(wired) == ["Brand New"]

    def test_draining_again_publishes_nothing_further(self, wired):
        _pair(wired, "Drain Twice")
        job = wired.discover()[0]
        wired.drain()
        wired.drain()
        wired.drain()
        assert _titles(wired) == ["Drain Twice"], "a repeated drain republished"
        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED


# ---------------------------------------------------------------------------
# C. local job missing + remote episode exists
# ---------------------------------------------------------------------------
class TestC_RemoteOnly:
    """A job that has never been sent does not adopt on a title match.

    This is a deliberate trade-off. A same-titled episode on the show could
    be the same recording or a different one, and a title cannot tell those
    apart. Adopting it would silently discard the file the user just dropped
    in; publishing it creates a visible duplicate the user can delete, and
    the job records a warning saying why. Losing someone's recording is the
    worse failure, so the file is never dropped.
    """

    def test_a_first_run_publishes_and_records_a_title_warning(self, wired, config):
        wired.spotify.seed_episode("Already There", state="published")
        assert wired.db.list_jobs(limit=50) == []

        _pair(wired, "Already There")
        jobs = wired.discover()
        wired.drain()

        assert len(jobs) == 1
        # Re-read: the row captured at discover() still says DISCOVERED.
        job = wired.db.get_job(jobs[0].id)
        assert job.state_enum() == JobState.ARCHIVED, job.state
        # The pre-existing one, plus this one - a visible duplicate the user
        # can delete, not a silently dropped file.
        assert _titles(wired) == ["Already There", "Already There"]

        events = " ".join(e["message"] for e in wired.db.get_events(job.id))
        assert "Spotify has 1 episode(s) titled 'Already There'" in events, (
            f"the user was not told about the clash:\n{events}"
        )
        assert "treated as a different recording" in events, (
            f"the clash was not explained:\n{events}"
        )

    def test_two_different_files_with_the_same_title_are_both_published(self, wired):
        """Two genuinely different recordings, deliberately given one title."""
        wired.spotify.seed_episode("Same Title", state="published")
        # Distinct file names, so both files really are in the folder at once.
        wired.drop_media("Take One")
        make_mp3(wired.incoming / "Take Two.mp3", seconds=2.0, comment="a different take")
        for stem in ("Take One", "Take Two"):
            wired.drop(f"{stem}.txt", "TITLE=Same Title\nDESCRIPTION=Two takes.\n")

        jobs = wired.discover()
        assert len(jobs) == 2, "the two different files must produce two jobs"
        assert len({j.content_hash for j in jobs}) == 2

        wired.drain()

        # One that was already there, plus one per new recording.
        assert _titles(wired) == ["Same Title"] * 3, (
            f"a different recording was swallowed: "
            f"{[e.title for e in wired.spotify.episodes()]}"
        )
        published = {wired.db.get_job(j.id).spotify_episode_id for j in jobs}
        assert len(published) == 2, f"both new episodes must be distinct: {published}"


# ---------------------------------------------------------------------------
# D. upload succeeded, local process crashed
# ---------------------------------------------------------------------------
class TestD_CrashAfterUpload:
    def test_recovery_adopts_the_uploaded_episode(self, wired, config):
        _pair(wired, "Crashed After Upload")
        job = wired.discover()[0]
        # The upload reached Spotify, then the process died.
        wired.spotify.seed_episode("Crashed After Upload", state="published")
        wired.force_state(job.id, JobState.UPLOADING, message="crashed mid-upload")
        assert wired.db.get_job(job.id).state_enum() == JobState.UPLOADING

        wired.db.recover_interrupted_jobs()
        wired.drain()

        row = wired.db.get_job(job.id)
        assert row.state_enum() == JobState.ARCHIVED, row.state
        assert _titles(wired) == ["Crashed After Upload"], "the crash caused a duplicate"

    def test_a_crash_with_no_remote_episode_publishes_once(self, wired, config):
        _pair(wired, "Crashed Before Upload")
        job = wired.discover()[0]
        wired.force_state(job.id, JobState.UPLOADING, message="crashed mid-upload")
        assert wired.spotify.episodes() == []

        wired.db.recover_interrupted_jobs()
        wired.drain()

        assert _titles(wired) == ["Crashed Before Upload"]
        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED


# ---------------------------------------------------------------------------
# E. publication succeeded, local response lost
# ---------------------------------------------------------------------------
class TestE_LostResponse:
    def test_the_episode_is_adopted_not_published_again(self, wired, config):
        """Spotify created the episode; we never saw the reply."""
        _pair(wired, "Lost Response")
        job = wired.discover()[0]
        wired.spotify.seed_episode("Lost Response", state="published")
        # PUBLISHING is where the response would have been read.
        wired.force_state(job.id, JobState.PUBLISHING, message="response lost")

        wired.db.recover_interrupted_jobs()
        wired.drain()

        assert _titles(wired) == ["Lost Response"]
        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED

    def test_recovery_is_idempotent_when_run_twice(self, wired, config):
        _pair(wired, "Double Recovery")
        job = wired.discover()[0]
        wired.spotify.seed_episode("Double Recovery", state="published")
        wired.force_state(job.id, JobState.PUBLISHING, message="response lost")

        wired.db.recover_interrupted_jobs()
        wired.db.recover_interrupted_jobs()
        wired.drain()

        assert _titles(wired) == ["Double Recovery"]
        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED


# ---------------------------------------------------------------------------
# F. the remote side rejected the episode
# ---------------------------------------------------------------------------
class TestF_RemoteRejection:
    def test_a_rejection_fails_visibly_and_is_retryable(self, wired):
        wired.spotify.state.form_error = "Rejected for testing."
        _pair(wired, "Rejected Episode")
        job = wired.discover()[0]

        wired.drain()

        row = wired.db.get_job(job.id)
        assert row.state_enum() in (JobState.FAILED, JobState.RETRY_WAIT), row.state
        assert "Rejected" in (row.last_error or ""), row.last_error
        assert wired.spotify.episodes() == [], "a rejected episode was recorded as published"

    def test_the_retry_after_clearing_the_error_succeeds_once(self, wired):
        wired.spotify.state.form_error = "Rejected for testing."
        _pair(wired, "Retry After Reject")
        job = wired.discover()[0]
        wired.drain()
        assert wired.db.get_job(job.id).state_enum() in (
            JobState.FAILED, JobState.RETRY_WAIT)

        wired.spotify.state.form_error = ""
        wired.service.retry_job(job.id, now=True)
        wired.drain()

        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED
        assert _titles(wired) == ["Retry After Reject"]


# ---------------------------------------------------------------------------
# G. the archive operation failed
# ---------------------------------------------------------------------------
class TestG_ArchiveFailure:
    def test_the_job_stays_published_and_is_never_republished(self, wired):
        import app.archive.manager as manager_module

        _pair(wired, "Archive Failure")
        job = wired.discover()[0]
        wired.force_state(job.id, JobState.PUBLISHED, message="published")

        original = manager_module._move
        manager_module._move = lambda *a, **k: (_ for _ in ()).throw(OSError("locked"))
        try:
            _worker(wired)._archive(job.id)
        finally:
            manager_module._move = original

        row = wired.db.get_job(job.id)
        assert row.state_enum() == JobState.PUBLISHED, (
            f"an archive failure moved the job to {row.state}"
        )
        assert not row.get("archived_media_path")

        # Retrying the archive must not publish again.
        before = _titles(wired)
        _worker(wired)._archive(job.id)
        assert wired.db.get_job(job.id).state_enum() == JobState.ARCHIVED
        assert _titles(wired) == before == []


# ---------------------------------------------------------------------------
# H, I, K. the same content, however it is named
# ---------------------------------------------------------------------------
class TestHIK_SameContentDifferentNames:
    def test_h_dropping_the_exact_same_files_again_changes_nothing(self, wired):
        _pair(wired, "Same Files")
        job = wired.discover()[0]
        wired.drain()
        assert _titles(wired) == ["Same Files"]

        _pair(wired, "Same Files")           # identical bytes, identical name
        assert len(wired.discover()) == 1, "a duplicate job was created"
        wired.drain()
        assert _titles(wired) == ["Same Files"], "a duplicate episode was published"

    def test_i_the_same_media_under_a_different_name_is_not_published_twice(self, wired):
        _pair(wired, "Original Name")
        job = wired.discover()[0]
        wired.drain()
        assert _titles(wired) == ["Original Name"]

        # The identical bytes under a different name.
        media = make_mp3(wired.incoming / "Renamed.mp3", seconds=2.0,
                         comment="episode Original Name")
        (wired.incoming / "Renamed.txt").write_text(
            "TITLE=Original Name\nDESCRIPTION=A description for the test episode.\n",
            encoding="utf-8",
        )
        jobs = wired.discover()
        assert len(jobs) == 1, "a second job was created for the same content"
        assert jobs[0].id == job.id, "identity followed the file name instead of the content"

        wired.drain()
        assert _titles(wired) == ["Original Name"], "the rename caused a second publication"

    def test_k_a_different_title_with_the_same_media_is_still_one_episode(self, wired):
        _pair(wired, "First Title")
        job = wired.discover()[0]
        wired.drain()

        # Same bytes, a different title in the .txt.
        media = make_mp3(wired.incoming / "Second Title.mp3", seconds=2.0,
                         comment="episode First Title")
        (wired.incoming / "Second Title.txt").write_text(
            "TITLE=Second Title\nDESCRIPTION=A description for the test episode.\n",
            encoding="utf-8",
        )
        jobs = wired.discover()
        assert len(jobs) == 1
        assert jobs[0].id == job.id

        wired.drain()
        assert _titles(wired) == ["First Title"], (
            "content identity must win over a changed title"
        )


# ---------------------------------------------------------------------------
# J. the same title, different media
# ---------------------------------------------------------------------------
class TestJ_SameTitleDifferentMedia:
    def test_both_are_published(self, wired):
        _pair(wired, "Rec Episode 1")
        first = wired.discover()[0]
        wired.drain()

        # A genuinely different recording, deliberately given the same title.
        make_mp3(wired.incoming / "Rec Episode 2.mp3", seconds=2.0, comment="a different take")
        (wired.incoming / "Rec Episode 2.txt").write_text(
            "TITLE=Rec Episode 1\nDESCRIPTION=A description for the test episode.\n",
            encoding="utf-8",
        )
        jobs = wired.discover()
        assert len(jobs) == 2, "a different recording with the same title was swallowed"
        assert jobs[1].content_hash != jobs[0].content_hash

        wired.drain()
        assert _titles(wired) == ["Rec Episode 1", "Rec Episode 1"], (
            "two different recordings should both be published"
        )


# ---------------------------------------------------------------------------
# The durable identity itself
# ---------------------------------------------------------------------------
class TestIdentityStrategy:
    def test_identity_is_the_content_hash_not_the_name(self, wired):
        _pair(wired, "Identity Check")
        job = wired.discover()[0]
        assert job.content_hash, "a job must carry a content hash"
        assert job.media_hash, "a job must carry a media hash"

    def test_the_hash_survives_a_restart(self, wired, config):
        _pair(wired, "Hash Survives")
        job = wired.discover()[0]
        original = job.content_hash
        wired.db.close()

        reopened = Database(config.database_path())
        try:
            row = reopened.get_job(job.id)
            assert row.content_hash == original
        finally:
            reopened.close()

    def test_two_different_files_never_share_a_hash(self, wired):
        a = make_mp3(wired.incoming / "A.mp3", seconds=2.0, comment="a")
        b = make_mp3(wired.incoming / "B.mp3", seconds=2.0, comment="b")
        wired.drop("A.txt", "TITLE=A\nDESCRIPTION=d\n")
        wired.drop("B.txt", "TITLE=B\nDESCRIPTION=d\n")
        jobs = wired.discover()
        assert len(jobs) == 2
        assert jobs[0].content_hash != jobs[1].content_hash


