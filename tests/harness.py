"""Shared fixtures for the full folder -> Spotify integration matrix.

These tests drive the *production* pipeline end to end: the real watcher, the
real metadata parser, the real media validator, the real SQLite database, the
real worker, the real :class:`SpotifyPublisher` and the real archive manager.
The only substitution is the browser: :class:`FakeSession` performs genuine
HTTP against :class:`MockSpotify`, so form submission, file upload, redirects
and the episode list are all real requests and real server-side state.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.core.queue import JobWorker


class Harness:
    """Thin test-side driver.  It calls production APIs; it replaces none."""

    def __init__(self, config, database, spotify, service, adapter, sessions):
        self.config = config
        self.db = database
        self.spotify = spotify
        self.service = service
        self.adapter = adapter
        self.sessions = sessions
        self.watcher = service.watcher
        self.watcher.register_shows()
        self.worker = JobWorker(
            config, database, adapter=adapter, worker_id="worker-1",
            archive_manager=service.archive,
        )

    # -- input ------------------------------------------------------------
    @property
    def show(self):
        return self.config.shows[0]

    @property
    def incoming(self) -> Path:
        return self.config.show_incoming(self.show)

    def drop(self, name: str, content: bytes | str = b"x" * 2048) -> Path:
        """Copy a file into ``incoming`` exactly as a user dragging one would."""
        self.incoming.mkdir(parents=True, exist_ok=True)
        target = self.incoming / name
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_bytes(content)
        return target

    def drop_media(self, base_name: str, *, kind: str = "mp3", seconds: float = 2.0):
        """Drop a *distinct* media file for *base_name*.

        The distinctness matters: the app identifies an episode by content
        hash, so two byte-identical files are correctly treated as the same
        episode.  The ID3 comment is what makes this fixture unique.
        """
        from make_test_media import make_mp3, make_mp4

        self.incoming.mkdir(parents=True, exist_ok=True)
        target = self.incoming / f"{base_name}.{kind}"
        if kind == "mp4":
            return make_mp4(target, seconds=seconds)
        return make_mp3(target, seconds=seconds, comment=f"episode {base_name}")

    def drop_metadata(self, base_name: str, **fields) -> Path:
        """Write a valid ``.txt``.

        Keys given in *fields* **replace** the default of the same name.
        Prepending them instead would append them to the preceding value,
        because an unindented line continues the previous one.
        """
        values: dict[str, str] = {
            "TITLE": "Test Episode",
            "DESCRIPTION": "A description for the test episode.",
        }
        for key, value in fields.items():
            values[key.upper()] = str(value)
        body = "\n".join(f"{key}={value}" for key, value in values.items())
        return self.drop(f"{base_name}.txt", body + "\n")

    def drop_pair(self, base_name: str, *, kind: str = "mp3", **fields) -> tuple[Path, Path]:
        return self.drop_media(base_name, kind=kind), self.drop_metadata(base_name, **fields)

    def discover(self, timeout: float = 15.0, expect: int | None = None) -> list:
        """Run the real watcher over ``incoming`` and queue what it finds.

        This is the production path: ``FilesystemWatcher.scan_once`` does the
        stability check, the pairing, the "waiting for .txt" bookkeeping and
        the dedup of already-emitted files, then hands the pair to the service.
        """
        # Each file needs its own stability window, and a sweep only settles
        # what it has polled once, so keep sweeping until the folder stops
        # producing new jobs (or *expect* jobs have appeared).
        deadline = time.time() + timeout
        settled_since = None
        last = -1
        while time.time() < deadline:
            self.watcher.scan_once()
            count = len(self.db.list_jobs(limit=200))
            if expect is not None and count >= expect:
                break
            if count != last:
                last = count
                settled_since = None
            elif settled_since is None:
                settled_since = time.time()
            elif time.time() - settled_since > self.config.watcher.stable_seconds + 0.3:
                break
            time.sleep(0.05)
        return list(self.db.list_jobs(limit=200))

    def discover_one(self, base_name: str = "Solo", **kwargs):
        """Drop a single pair and return the job it becomes."""
        self.drop_pair(base_name, **kwargs)
        jobs = self.discover()
        assert len(jobs) == 1, f"expected one job, found {len(jobs)}"
        return jobs[0]

    def force_state(self, job_id: str, state, *, message: str = "", **fields):
        """Drive a job into *state* directly, bypassing the happy path.

        Only used to set up failure and recovery scenarios.
        """
        from app.core.database import JobState as _S

        return self.db.transition(job_id, _S(state) if not isinstance(state, _S) else state,
                                  message=message or "forced by the test",
                                  force=True, **fields)

    def media_files(self):
        return sorted(self.incoming.glob("*.mp3")) + sorted(self.incoming.glob("*.mp4"))

    def settle(self, seconds: float | None = None) -> None:
        """Let the stability window elapse, as the background thread would."""
        time.sleep(seconds if seconds is not None
                   else self.config.watcher.stable_seconds + 0.2)

    def events(self, kind: str = "") -> list[dict]:
        found = self.watcher.recent_events(limit=200)
        return [e for e in found if not kind or e["kind"] == kind]

    # -- execution --------------------------------------------------------
    def drain(self, *, limit: int = 40) -> list[str]:
        """Run the worker until the queue is empty, as the pool would."""
        handled = []
        for _ in range(limit):
            job_id = self.worker.run_once()
            if job_id is None:
                break
            handled.append(job_id)
        return handled

    def states_of(self, job_id: str) -> list[str]:
        return [e["state"] for e in self.db.get_events(job_id) if e["state"]]

    def job(self, job_id: str):
        return self.db.get_job(job_id)

    def episode_titles(self) -> list[str]:
        return [e.title for e in self.spotify.episodes()]


# ------------------------------------------------------- metadata helper

GOOD_METADATA = """\
TITLE=Test Episode
SEASON=2
EPISODE=7
EXPLICIT=no
PUBLISH=now
CHAPTERS=
00:00 Intro
01:00 Main topic
02:00 Outro
DESCRIPTION=A description that Spotify will show under the episode.
"""


def write_metadata(path: Path, text: str = GOOD_METADATA) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
