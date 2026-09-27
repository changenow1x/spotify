"""CASE A / B -- the happy path, end to end, through production code only.

``incoming/TEST.mp4`` + ``incoming/TEST.txt`` must travel the whole pipeline and
come out as a single published, verified and archived episode.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from app.core.state_machine import JobState
from tests.harness import GOOD_METADATA


class TestVideoEpisodeEndToEnd:
    def test_full_workflow(self, wired):
        wired.drop("TEST.txt", GOOD_METADATA)
        wired.drop_media("TEST", kind="mp4", seconds=2.0)

        jobs = wired.discover()
        assert len(jobs) == 1, "exactly one job may be created per episode"
        job_id = jobs[0]["id"]

        wired.drain()

        job = wired.job(job_id)
        assert job.state_enum() is JobState.ARCHIVED, (
            f"ended in {job['state']}: {job['last_error']}")

        # --- every state was visited, in order ----------------------------
        seen = [s for s in wired.states_of(job_id) if s]
        for state in ("DISCOVERED", "VALIDATING", "VALIDATED", "UPLOADING",
                      "PROCESSING", "READY_TO_PUBLISH", "PUBLISHING", "PUBLISHED"):
            assert state in seen, f"{state} was never reached (saw {seen})"

        # --- exactly one episode exists on Spotify -------------------------
        episodes = wired.spotify.episodes()
        assert len(episodes) == 1
        episode = episodes[0]
        assert episode.title == "Test Episode"
        assert episode.state == "published"

        # --- the details really arrived -----------------------------------
        assert episode.season == 2
        assert episode.episode_number == 7
        assert episode.is_explicit is False
        assert "Spotify will show" in episode.description
        for stamp in ("00:00", "01:00", "02:00"):
            assert stamp in episode.description, f"chapter {stamp} was lost"

        # --- the media file really was uploaded ----------------------------
        uploads = [line for line in wired.spotify.state.log if "TEST.mp4" in line]
        assert uploads, f"the file was never sent: {wired.spotify.state.log}"

        # --- the remote identity was recorded ------------------------------
        assert job["spotify_episode_id"] == episode.episode_id
        assert job["spotify_state"] == "published"
        assert job["spotify_url"]
        assert job["publish_confirmed_at"]
        assert job["progress_percent"] == 100.0

        # --- the files were archived, not deleted --------------------------
        assert not wired.incoming.exists() or not list(wired.incoming.iterdir())
        from app.security.paths import is_within

        archived_media = Path(job["archived_media_path"])
        assert archived_media.exists()
        assert archived_media.name == "TEST.mp4"
        assert is_within(wired.config.show_path(wired.show, "archive"), archived_media)
        assert Path(job["archived_metadata_path"]).exists()
        assert job["archived_at"]

    def test_no_duplicate_publication(self, wired):
        wired.drop("TEST.txt", GOOD_METADATA)
        wired.drop_media("TEST", kind="mp4")
        wired.discover()
        wired.drain()
        assert len(wired.spotify.episodes()) == 1

        # The worker runs again with an empty queue; nothing may be re-sent.
        wired.drain()
        assert len(wired.spotify.episodes()) == 1

    def test_progress_is_reported_while_uploading(self, wired):
        seen: list[dict] = []
        wired.worker.on_progress(lambda job_id, data: seen.append(data))
        wired.drop("TEST.txt", GOOD_METADATA)
        wired.drop_media("TEST", kind="mp4")
        job_id = wired.discover()[0]["id"]
        wired.drain()

        stages = [e["stage"] for e in seen]
        for expected in ("authenticate", "reconcile", "upload", "processing",
                         "details", "publishing", "confirming"):
            assert expected in stages, f"stage {expected} never reported: {stages}"
        assert all(e.get("at") for e in seen)


class TestAudioEpisodeEndToEnd:
    def test_full_workflow(self, wired):
        wired.drop("TEST.txt", GOOD_METADATA)
        wired.drop_media("TEST", kind="mp3", seconds=2.0)

        job_id = wired.discover()[0]["id"]
        wired.drain()

        job = wired.job(job_id)
        assert job.state_enum() is JobState.ARCHIVED, (
            f"ended in {job['state']}: {job['last_error']}")
        assert job["media_kind"] == "audio"

        episodes = wired.spotify.episodes()
        assert len(episodes) == 1
        assert episodes[0].title == "Test Episode"
        assert Path(job["archived_media_path"]).name == "TEST.mp3"

    def test_explicit_flag_is_carried(self, wired):
        wired.drop("TEST.txt", GOOD_METADATA.replace("EXPLICIT=no", "EXPLICIT=yes"))
        wired.drop_media("TEST", kind="mp3")
        wired.discover()
        wired.drain()
        assert wired.spotify.episodes()[0].is_explicit is True


class TestUnicodeContent:
    def test_german_and_emoji_titles_survive_the_round_trip(self, wired):
        meta = GOOD_METADATA.replace(
            "TITLE=Test Episode",
            "TITLE=Über uns – Folge 12 🎧 „Der Schlüssel“")
        wired.drop("TEST.txt", meta)
        wired.drop_media("TEST", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()

        assert wired.spotify.episodes()[0].title == "Über uns – Folge 12 🎧 „Der Schlüssel“"
        assert wired.job(job_id).state_enum() is JobState.ARCHIVED


class TestFailurePaths:
    def test_missing_title_stops_before_any_upload(self, wired):
        wired.drop("TEST.txt", "DESCRIPTION=No title here.\n")
        wired.drop_media("TEST", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()

        job = wired.job(job_id)
        assert job.state_enum() is JobState.NEEDS_USER_ACTION
        assert "TITLE" in (job["last_error"] or "")
        assert wired.spotify.episodes() == [], "nothing may be published"
        # The user's files are untouched so they can fix and re-drop.
        assert Path(job["media_path"]).exists()

    def test_missing_description_stops_publication(self, wired):
        wired.drop("TEST.txt", "TITLE=Only a title\n")
        wired.drop_media("TEST", kind="mp3")
        wired.discover()
        wired.drain()
        assert wired.spotify.episodes() == []

    def test_malformed_metadata_is_rejected_with_a_reason(self, wired):
        wired.drop("TEST.txt", "TITLE=Broken\nSEASON=not-a-number\nDESCRIPTION=d\n")
        wired.drop_media("TEST", kind="mp3")
        job_id = wired.discover()[0]["id"]
        wired.drain()
        job = wired.job(job_id)
        assert job.state_enum() is JobState.NEEDS_USER_ACTION
        assert job["last_error"]
        assert wired.spotify.episodes() == []

    def test_media_only_is_refused_by_default(self, wired):
        """The safe default: no .txt means no publication, and the user is told."""
        wired.drop_media("TEST", kind="mp3")
        assert wired.discover(timeout=1.0) == []
        wired.drain()
        assert wired.spotify.episodes() == []

        waiting = wired.events("waiting_metadata")
        assert waiting, "the user must be told what is missing"
        assert "TEST.txt" in waiting[0]["message"]

    def test_media_only_is_allowed_when_configured(self, wired):
        wired.config.media.allow_media_only = True
        wired.drop_media("TEST", kind="mp3")
        wired.discover()
        wired.drain()
        assert len(wired.spotify.episodes()) == 1
        assert wired.db.list_jobs()[0].state_enum() is JobState.ARCHIVED




