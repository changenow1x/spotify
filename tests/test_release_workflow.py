"""Release gate: the exact folder workflow, from a clean temporary folder.

    SHOW/
    incoming/
    Episode.mp4
    Episode.txt

Dropping those two files in must produce a published, archived episode with
no manual step in between.  This file is evidence for the release report, so
it asserts each stage rather than only the end state.
"""

from __future__ import annotations

import shutil

import pytest

from tests.harness import Harness

#: Exactly what a user writes, and exactly what the validator accepts.
USER_METADATA = """\
TITLE=Test Episode
DESCRIPTION=A description that Spotify will show under the episode.
SEASON=1
EPISODE=1
EXPLICIT=no
PUBLISH=NOW
CHAPTERS=
00:00 Intro
01:00 The main topic
02:00 Outro
"""


def test_exact_user_workflow_from_a_clean_folder(harness: Harness):
    """SHOW/incoming/Episode.mp4 + Episode.txt -> a published Spotify episode."""
    incoming = harness.incoming
    incoming.mkdir(parents=True, exist_ok=True)
    assert list(incoming.iterdir()) == [], "the folder must start empty"

    # -- the user drops two files whose names match ------------------------
    media = incoming / "Episode.mp4"
    text = incoming / "Episode.txt"
    harness.drop_media("Episode", kind="mp4")
    text.write_text(USER_METADATA, encoding="utf-8")

    # -- the watcher finds the pair and creates exactly one job ------------
    jobs = harness.discover()
    assert len(jobs) == 1, f"one pair of files must make one job, got {len(jobs)}"
    job_id = jobs[0]["id"]

    # -- the queue publishes it and the publication is confirmed -----------
    harness.drain()
    job = harness.job(job_id)

    # The mock was actually contacted, and the file was really sent.
    assert harness.spotify.state.log, "the mock was never contacted"
    assert any("Episode.mp4" in line for line in harness.spotify.state.log), (
        f"the file was never uploaded: {harness.spotify.state.log}")

    # Exactly one episode exists remotely, and it is published.
    episodes = harness.spotify.episodes()
    assert len(episodes) == 1, f"exactly one episode, got {len(episodes)}"
    episode = episodes[0]
    assert episode.state == "published"
    assert episode.title == "Test Episode"

    # The details really arrived, including the chapters.
    assert episode.season == 1
    assert episode.episode_number == 1
    assert episode.is_explicit is False
    for stamp in ("00:00", "01:00", "02:00"):
        assert stamp in episode.description, f"chapter {stamp} was lost"

    # The remote identity is persisted, both halves of it.
    job = harness.job(job_id)
    assert job["spotify_episode_id"] == episode.episode_id
    assert job["spotify_url"], "the Spotify URL was not persisted"

    # The archive succeeded.
    assert job.state_enum().name == "ARCHIVED", job["state"]
    archive_root = incoming.parent / "archive"
    assert archive_root.exists(), "nothing was archived"
    archived = sorted(p.name for p in archive_root.rglob("Episode.*"))
    assert archived == ["Episode.mp4", "Episode.txt"], archived

    # The files are no longer pending work.
    assert not media.exists(), "the media file is still in incoming"
    assert not text.exists(), "the metadata file is still in incoming"
    assert list(incoming.iterdir()) == []
    assert [j for j in harness.db.list_jobs(limit=50) if j["state"] == "QUEUED"] == []


def test_a_restart_after_the_workflow_publishes_nothing_more(harness: Harness):
    """Stop the app, start it again, and nothing is republished."""
    incoming = harness.incoming
    incoming.mkdir(parents=True, exist_ok=True)
    harness.drop_media("Episode", kind="mp4")
    (incoming / "Episode.txt").write_text(USER_METADATA, encoding="utf-8")

    jobs = harness.discover()
    job_id = jobs[0]["id"]
    harness.drain()
    assert len(harness.spotify.episodes()) == 1
    assert harness.job(job_id).state_enum().name == "ARCHIVED"
    first_id = harness.job(job_id)["spotify_episode_id"]

    # ---- restart: recovery runs over the same database -------------------
    harness.db.recover_interrupted_jobs()

    # The folder is empty, so a full scan creates no new work and the
    # existing job is left exactly as it was.
    before = {j["id"] for j in harness.db.list_jobs(limit=50)}
    harness.discover()
    after_jobs = harness.db.list_jobs(limit=50)
    assert {j["id"] for j in after_jobs} == before, "a restart created a new job"
    assert [j["state"] for j in after_jobs] == ["ARCHIVED"], after_jobs
    harness.drain()

    # The decisive assertion: still exactly one episode, the same one.
    episodes = harness.spotify.episodes()
    assert len(episodes) == 1, f"a restart republished: {len(episodes)} episodes"
    assert episodes[0].episode_id == first_id
    assert harness.job(job_id).state_enum().name == "ARCHIVED"


def test_dropping_the_same_bytes_again_is_not_published_twice(harness: Harness):
    """The user restores the same file under a new name: still one episode."""
    incoming = harness.incoming
    incoming.mkdir(parents=True, exist_ok=True)
    harness.drop_media("Episode", kind="mp4")
    (incoming / "Episode.txt").write_text(USER_METADATA, encoding="utf-8")
    harness.discover()
    harness.drain()
    assert len(harness.spotify.episodes()) == 1

    archive_root = incoming.parent / "archive"
    archived_media = next(archive_root.rglob("Episode.mp4"))

    # Same bytes, different name - a real duplicate ingest.
    shutil.copy2(archived_media, incoming / "Episode-copy.mp4")
    (incoming / "Episode-copy.txt").write_text(USER_METADATA, encoding="utf-8")

    before = {j["id"] for j in harness.db.list_jobs(limit=50)}
    rediscovered = harness.discover()
    after = {j["id"] for j in harness.db.list_jobs(limit=50)}

    # The same content hash resolves to the job that already exists, so no
    # second job is created - the identity is the content, not the name.
    assert after == before, "the same bytes were treated as a new episode"
    assert {j["id"] for j in rediscovered} <= before, (
        f"a new job was created for the same content: {rediscovered}")
    harness.drain()

    assert len(harness.spotify.episodes()) == 1, "the same bytes published twice"


@pytest.mark.parametrize("kind", ["mp3", "mp4"])
def test_both_media_kinds_take_the_same_path(harness: Harness, kind: str):
    """Audio and video differ only in the file, not in the workflow."""
    incoming = harness.incoming
    incoming.mkdir(parents=True, exist_ok=True)
    harness.drop_media("Episode", kind=kind)
    (incoming / "Episode.txt").write_text(USER_METADATA, encoding="utf-8")

    jobs = harness.discover()
    assert len(jobs) == 1
    harness.drain()
    job = harness.job(jobs[0]["id"])

    assert job.state_enum().name == "ARCHIVED", job["state"]
    assert len(harness.spotify.episodes()) == 1
    assert job["spotify_episode_id"]
    assert list(incoming.iterdir()) == []
