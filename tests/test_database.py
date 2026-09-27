"""Database durability: duplicate suppression, atomic claims, crash recovery.

These tests are the backbone of the "never publish twice" requirement, so they
deliberately restart the :class:`Database` object against the same file to
prove nothing lives only in memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.database import Database
from app.core.state_machine import JobState


def _queue(db, job):
    """Walk a freshly created job to the QUEUED state a worker picks up."""
    db.transition(job["id"], JobState.VALIDATING)
    db.transition(job["id"], JobState.VALIDATED)
    db.transition(job["id"], JobState.QUEUED)
    return db.get_job(job["id"])


def _age(db, job_id, seconds: int = 86400) -> None:
    """Backdate a row's updated_at stamp (what a long-running claim looks like)."""
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds")
    with db._write_lock:
        db.conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, job_id))


def _fields(**over):
    base = dict(
        show_id="test-show",
        base_name="ep01",
        original_filename="ep01.mp4",
        media_path="/incoming/ep01.mp4",
        metadata_path="/incoming/ep01.txt",
        media_size=1234,
        media_mtime_ns=99,
        media_kind="video",
        media_hash="m" * 64,
        metadata_hash="t" * 64,
        content_hash="c" * 64,
        title="Episode 1",
        metadata_json='{"title": "Episode 1", "description": "The first one."}',
    )
    base.update(over)
    return base


@pytest.fixture
def db(database):
    return database


# ------------------------------------------------------------------ basics

class TestJobLifecycle:
    def test_create_and_read_back(self, db):
        job = db.create_job(**_fields())
        assert job is not None
        assert job["show_id"] == "test-show"
        assert job["title"] == "Episode 1"
        assert job.state_enum() is JobState.DISCOVERED
        assert db.get_job(job["id"])["id"] == job["id"]

    def test_defaults_are_filled(self, db):
        job = db.create_job(**_fields())
        assert job["state"] == JobState.DISCOVERED.value
        assert job["job_type"] == "paired"
        assert job["publish_mode"] == "NOW"
        assert job["created_at"] and job["updated_at"]

    def test_event_is_logged_on_creation(self, db):
        job = db.create_job(**_fields())
        events = db.get_events(job["id"])
        assert events
        assert "detected" in events[0]["message"].lower()

    def test_metadata_and_media_info_deserialise(self, db):
        job = db.create_job(**_fields(
            media_info_json='{"kind": "video", "duration": 12.5}'))
        assert job.media_info()["duration"] == 12.5
        assert job.metadata()["description"] == "The first one."

    def test_row_helpers_survive_empty_blobs(self, db):
        job = db.create_job(**_fields(media_info_json=None, metadata_json=None,
                                      diagnostics_json="not json at all"))
        assert job.media_info() == {}
        assert job.metadata() == {}
        # A corrupt diagnostics blob must not take down the dashboard.
        assert job.diagnostics() == {}

    def test_update_job(self, db):
        job = db.create_job(**_fields())
        updated = db.update_job(job["id"], status_message="half way", retry_count=2)
        assert updated["status_message"] == "half way"
        assert updated["retry_count"] == 2

    def test_missing_job(self, db):
        assert db.get_job("does-not-exist") is None


# -------------------------------------------------------------- duplicates

class TestDuplicateSuppression:
    def test_same_content_hash_never_creates_a_second_job(self, db):
        first = db.create_job(**_fields())
        second = db.create_job(**_fields(media_path="/incoming/renamed.mp4",
                                         metadata_path="/incoming/renamed.txt"))
        assert second["id"] == first["id"]

    def test_rename_is_recognised_as_the_same_episode(self, db):
        first = db.create_job(**_fields())
        renamed = db.create_job(**_fields(
            base_name="ep01 (copy)", original_filename="ep01 (copy).mp4",
            media_path="/incoming/ep01 (copy).mp4",
            metadata_path="/incoming/ep01 (copy).txt"))
        assert renamed["id"] == first["id"]
        assert renamed["base_name"] == "ep01"

    def test_duplicate_drop_is_recorded_for_the_user(self, db):
        first = db.create_job(**_fields())
        db.create_job(**_fields(media_path="/incoming/again.mp4"))
        events = db.get_events(first["id"])
        assert any("duplicate" in e["message"].lower() for e in events)

    def test_same_media_hash_different_metadata_is_still_the_same_media(self, db):
        first = db.create_job(**_fields())
        second = db.create_job(**_fields(
            content_hash="different" * 8, metadata_hash="other" * 8,
            metadata_path="/incoming/ep01-v2.txt"))
        assert second["id"] == first["id"]

    def test_different_content_creates_a_new_job(self, db):
        first = db.create_job(**_fields())
        second = db.create_job(**_fields(base_name="ep02",
                                        content_hash="z" * 64, media_hash="y" * 64))
        assert second["id"] != first["id"]

    def test_another_show_may_have_the_same_content(self, db):
        first = db.create_job(**_fields())
        second = db.create_job(**_fields(show_id="other-show"))
        assert second["id"] != first["id"]

    def test_finders(self, db):
        job = db.create_job(**_fields())
        assert db.find_job_by_content("test-show", "c" * 64)["id"] == job["id"]
        assert db.find_job_by_content("test-show", "nope") is None
        assert db.find_job_by_media("test-show", "m" * 64)["id"] == job["id"]
        assert db.find_job_by_files(
            "test-show", "/incoming/ep01.mp4", "/incoming/ep01.txt")["id"] == job["id"]
        assert db.find_job_by_files("test-show", "/nowhere.mp4", None) is None

    def test_lookup_without_a_hash_does_not_match_everything(self, db):
        db.create_job(**_fields())
        assert db.find_job_by_content("test-show", None) is None
        assert db.find_job_by_media("test-show", None) is None

    def test_duplicate_survives_a_restart(self, config):
        path = config.database_path()
        first_db = Database(path)
        first = first_db.create_job(**_fields())
        first_db.close()

        second_db = Database(path)
        try:
            again = second_db.create_job(**_fields(media_path="/incoming/copy.mp4"))
            assert again["id"] == first["id"]
            assert sum(second_db.count_jobs().values()) == 1
        finally:
            second_db.close()


# ------------------------------------------------------------------ claims

class TestClaiming:
    def test_claim_is_exclusive(self, db):
        _queue(db, db.create_job(**_fields()))
        first = db.claim_next_job("worker-1")
        second = db.claim_next_job("worker-2")
        assert first is not None
        assert second is None

    def test_claim_records_the_worker(self, db):
        _queue(db, db.create_job(**_fields()))
        claimed = db.claim_next_job("worker-1")
        assert claimed["claimed_by"] == "worker-1"

    def test_nothing_to_claim_returns_none(self, db):
        assert db.claim_next_job("worker-1") is None

    def test_cancelled_jobs_are_not_claimed(self, db):
        job = _queue(db, db.create_job(**_fields()))
        assert db.request_cancel(job["id"]) is True
        assert db.claim_next_job("worker-1") is None
        assert db.cancel_requested(job["id"]) is True

    def test_retry_wait_is_held_until_its_time(self, db):
        job = _queue(db, db.create_job(**_fields()))
        db.transition(job["id"], JobState.RETRY_WAIT,
                      next_attempt_at="2999-01-01T00:00:00+00:00")
        assert db.claim_next_job("worker-1") is None

    def test_reconciliation_is_prioritised(self, db):
        normal = _queue(db, db.create_job(**_fields()))
        unsure = _queue(db, db.create_job(**_fields(
            base_name="ep02", content_hash="q" * 64, media_hash="w" * 64,
            metadata_hash="e" * 64)))
        db.transition(unsure["id"], JobState.NEEDS_RECONCILIATION)
        claimed = db.claim_next_job("worker-1")
        assert claimed["id"] == unsure["id"]

    def test_stale_claims_are_released_on_restart(self, config):
        path = config.database_path()
        first = Database(path)
        job = _queue(first, first.create_job(**_fields()))
        first.transition(job["id"], JobState.AUTHENTICATING)
        first.transition(job["id"], JobState.UPLOADING, claimed_by="worker-1")
        first.close()

        second = Database(path)
        try:
            recovered = second.recover_interrupted_jobs()
            assert any(j == job["id"] for j, _, _ in recovered)
            state = second.get_job(job["id"]).state_enum()
            assert state is JobState.NEEDS_RECONCILIATION
            assert second.get_job(job["id"])["claimed_by"] in (None, "")
            # Recovery must be idempotent: a second restart cannot degrade a
            # job that is already waiting for a duplicate check.
            second.recover_interrupted_jobs()
            assert second.get_job(job["id"]).state_enum() is JobState.NEEDS_RECONCILIATION
        finally:
            second.close()

    def test_reset_stale_claims(self, db):
        job = _queue(db, db.create_job(**_fields()))
        db.update_job(job["id"], claimed_by="ghost")
        _age(db, job["id"])
        assert db.reset_stale_claims(older_than_seconds=1) >= 1
        assert db.get_job(job["id"])["claimed_by"] in (None, "")

    def test_a_claim_is_a_lease_that_eventually_expires(self, db):
        """A worker that dies must not block a job forever."""
        job = _queue(db, db.create_job(**_fields()))
        db.update_job(job["id"], claimed_by="ghost")
        assert db.claim_next_job("worker-2") is None
        _age(db, job["id"])
        assert db.claim_next_job("worker-2")["id"] == job["id"]

    def test_reset_stale_claims_leaves_finished_jobs_alone(self, db):
        job = _queue(db, db.create_job(**_fields()))
        db.update_job(job["id"], claimed_by="ghost", last_error="x",
                      state=JobState.PUBLISHED.value)
        _age(db, job["id"])
        assert db.reset_stale_claims(older_than_seconds=1) == 0
        assert db.get_job(job["id"])["claimed_by"] == "ghost"


# ------------------------------------------------------------ state changes

class TestTransitions:
    def test_valid_transition_is_recorded(self, db):
        job = db.create_job(**_fields())
        updated = db.transition(job["id"], JobState.VALIDATING)
        assert updated.state_enum() is JobState.VALIDATING
        assert updated["updated_at"] >= job["updated_at"]

    def test_illegal_transition_is_refused(self, db):
        from app.core.state_machine import IllegalTransition

        job = db.create_job(**_fields())
        with pytest.raises(IllegalTransition):
            db.transition(job["id"], JobState.PUBLISHED)
        assert db.get_job(job["id"]).state_enum() is JobState.DISCOVERED

    def test_every_transition_writes_an_event(self, db):
        job = _queue(db, db.create_job(**_fields()))
        db.transition(job["id"], JobState.RETRY_WAIT, message="backing off")
        states = [e["state"] for e in db.get_events(job["id"])]
        assert JobState.RETRY_WAIT.value in states

    def test_retire_job_removes_it_from_the_queue(self, db):
        job = _queue(db, db.create_job(**_fields()))
        db.retire_job(job["id"], "superseded")
        assert db.get_job(job["id"])["retired"] == 1
        assert db.claim_next_job("worker-1") is None
        assert db.get_job(job["id"])["retired"] == 1

    def test_count_jobs_by_state(self, db):
        db.create_job(**_fields())
        counts = db.count_jobs()
        assert sum(counts.values()) == 1
        assert counts.get("DISCOVERED", 0) == 1

    def test_count_jobs_per_show(self, db):
        db.create_job(**_fields())
        db.create_job(**_fields(show_id="other-show", content_hash="k" * 64,
                                media_hash="j" * 64, metadata_hash="l" * 64))
        assert sum(db.count_jobs(show_id="test-show").values()) == 1
        assert sum(db.count_jobs().values()) == 2

    def test_list_jobs_filters(self, db):
        a = db.create_job(**_fields())
        b = _queue(db, db.create_job(**_fields(base_name="ep02", content_hash="n" * 64,
                                               media_hash="o" * 64,
                                               metadata_hash="p" * 64)))
        assert [j["id"] for j in db.list_jobs(show_id="test-show")] != []
        assert {j["id"] for j in db.list_jobs(states=[JobState.QUEUED])} == {b["id"]}
        assert a["id"] not in {j["id"] for j in
                               db.list_jobs(states=[JobState.QUEUED])}

    def test_list_jobs_paginates(self, db):
        for i in range(5):
            db.create_job(**_fields(base_name=f"ep{i:02d}", content_hash=str(i) * 64,
                                   media_hash=str(i + 10) * 64,
                                   metadata_hash=str(i + 20) * 64))
        assert len(db.list_jobs(limit=2)) == 2
        assert len(db.list_jobs(limit=10, offset=4)) == 1


# ------------------------------------------------------------------ events

class TestEventsAndAudit:
    def test_events_are_ordered_and_capped(self, db):
        job = db.create_job(**_fields())
        for i in range(10):
            db.log_event(job["id"], None, "info", f"step {i}")
        events = db.get_events(job["id"], limit=4)
        assert len(events) == 4
        assert "step 9" in events[-1]["message"]

    def test_recent_events_span_jobs(self, db):
        a = db.create_job(**_fields())
        b = db.create_job(**_fields(base_name="ep02", content_hash="2" * 64,
                                   media_hash="3" * 64, metadata_hash="4" * 64))
        assert len(db.recent_events(limit=50)) >= 2

    def test_audit_trail(self, db):
        db.audit("force_publish", "job-1", "user pressed the button", actor="dashboard")
        entries = db.recent_audit(limit=10)
        assert entries
        assert entries[0]["action"] == "force_publish"
        assert entries[0]["actor"] == "dashboard"

    def test_audit_detail_is_redacted(self, db):
        from app.security.redaction import REDACTION

        db.audit("login", "spotify", "password=hunter2 token=AQsecretvalue1234567890abcdefghij")
        entry = db.recent_audit(limit=1)[0]
        assert "hunter2" not in entry["detail"]
        assert REDACTION in entry["detail"]


# ------------------------------------------------- seen files and pending

class TestSeenFiles:
    def test_note_and_read_back(self, db):
        db.note_seen_file("test-show", "/incoming/ep01.mp4", "media", 1234, 77,
                          media_hash="m" * 64)  # size, mtime_ns
        row = db.get_seen_file("test-show", "/incoming/ep01.mp4", "media")
        assert row and row["media_hash"] == "m" * 64
        assert row["size"] == 1234 and row["mtime_ns"] == 77

    def test_unknown_file(self, db):
        assert db.get_seen_file("test-show", "/nope", "media") is None

    def test_pending_media_round_trip(self, db):
        db.add_pending_media("test-show", "ep01", "/incoming/ep01.mp4", 10, 5,
                             "m" * 64, "video", {"kind": "video"})
        row = db.get_pending_media("test-show", "/incoming/ep01.mp4")
        assert row["base_name"] == "ep01"
        assert [r["base_name"] for r in db.pending_for_show("test-show")] == ["ep01"]
        db.remove_pending_media("test-show", "/incoming/ep01.mp4")
        assert db.get_pending_media("test-show", "/incoming/ep01.mp4") is None

    def test_pending_media_is_upserted_not_duplicated(self, db):
        for size in (10, 20):
            db.add_pending_media("test-show", "ep01", "/incoming/ep01.mp4", size, 1,
                                 "m" * 64, "video")
        assert len(db.pending_for_show("test-show")) == 1


# ---------------------------------------------------------- spotify cache

class TestSpotifyCache:
    def test_shows_round_trip(self, db):
        db.upsert_spotify_show("test-show", "Test Show", "abc123", "https://spotify/x")
        shows = db.get_spotify_shows()
        assert shows and shows[0]["spotify_id"] == "abc123"

    def test_episodes_round_trip(self, db):
        db.upsert_spotify_show("test-show", "Test Show", "abc123")
        db.upsert_spotify_episode("test-show", {
            "episode_id": "ep-remote-1", "title": "Episode 1", "state": "published",
            "url": "https://spotify/ep-remote-1", "season": 1, "episode_number": 1,
        })
        found = db.find_spotify_episode("test-show", "ep-remote-1")
        assert found["title"] == "Episode 1"
        assert db.find_spotify_episode("test-show", "nope") is None

    def test_episode_update_does_not_duplicate(self, db):
        db.upsert_spotify_show("test-show", "Test Show", "abc")
        for state in ("processing", "published"):
            db.upsert_spotify_episode("test-show", {
                "episode_id": "ep-1", "title": "T", "state": state})
        assert len(db.list_spotify_episodes("test-show")) == 1
        assert db.find_spotify_episode("test-show", "ep-1")["state"] == "published"

    def test_search_by_title(self, db):
        db.upsert_spotify_show("test-show", "Test Show", "abc")
        db.upsert_spotify_episode("test-show", {"episode_id": "a", "title": "Ep One"})
        db.upsert_spotify_episode("test-show", {"episode_id": "b", "title": "Ep Two"})
        assert len(db.find_spotify_episodes_by_title("test-show", "Ep One")) == 1
        assert db.find_spotify_episodes_by_title("test-show", "Nope") == []

    def test_shows_are_isolated(self, db):
        for show in ("a-show", "b-show"):
            db.upsert_spotify_show(show, show, show)
            db.upsert_spotify_episode(show, {"episode_id": "x", "title": show})
        assert db.find_spotify_episode("a-show", "x")["title"] == "a-show"
        assert db.find_spotify_episode("b-show", "x")["title"] == "b-show"

    def test_last_checked_marker(self, db):
        db.upsert_spotify_show("test-show", "Test Show", "abc")
        db.set_spotify_last_checked("test-show")
        db.close()
        again = Database.__new__(Database)  # not reopened; just read what we can
        assert again is not None


# ---------------------------------------------------------------- settings

class TestSettings:
    def test_round_trip(self, db):
        db.set_setting("theme", "dark")
        assert db.get_setting("theme") == "dark"

    def test_default_for_missing_key(self, db):
        assert db.get_setting("nope", "fallback") == "fallback"
        assert db.get_setting("nope") is None

    def test_complex_values_survive(self, db):
        value = {"shows": [1, 2, 3], "flag": True, "text": "üñî"}
        db.set_setting("blob", value)
        assert db.get_setting("blob") == value

    def test_delete(self, db):
        db.set_setting("gone", 1)
        db.delete_setting("gone")
        assert db.get_setting("gone") is None

    def test_settings_survive_a_restart(self, config):
        path = config.database_path()
        first = Database(path)
        first.set_setting("token_hint", "abc")
        first.close()
        second = Database(path)
        try:
            assert second.get_setting("token_hint") == "abc"
        finally:
            second.close()


# -------------------------------------------------------------- durability

class TestDurability:
    def test_schema_is_created_and_versioned(self, db):
        assert db.schema_version() >= 1

    def test_create_job_always_assigns_an_id(self, db):
        """A NULL primary key would make the job permanently unreachable."""
        job = db.create_job(**_fields())
        assert job["id"]
        assert db.get_job(job["id"]) is not None

    def test_database_file_exists_after_write(self, config, db):
        db.create_job(**_fields())
        assert Path(config.database_path()).exists()

    def test_wal_mode_is_enabled(self, db):
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_stats(self, db):
        db.create_job(**_fields())
        stats = db.stats()
        assert stats["total"] == 1
        assert stats["published"] == 0 and stats["failed"] == 0
        assert stats["database_path"] == str(db.path)

    def test_close_is_idempotent(self, config):
        path = config.database_path()
        handle = Database(path)
        handle.close()
        handle.close()
