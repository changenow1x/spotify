"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from app.core.config import AppConfig, ShowConfig, load_config  # noqa: E402
from app.core.database import Database  # noqa: E402


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "config").mkdir(parents=True)
    return root


@pytest.fixture
def config(runtime_root: Path) -> AppConfig:
    path = runtime_root / "config" / "config.yaml"
    path.write_text(
        """
root: ""
hash_mode: quick
shows:
  - name: "Test Show"
    id: "test-show"
    enabled: true
    timezone: "Europe/Berlin"
    spotify_show_id: "mock-1"
    defaults:
      explicit: false
      publish: "NOW"
      timezone: "Europe/Berlin"
watcher:
  stable_seconds: 0.2
  poll_interval_seconds: 0.1
  stable_checks: 1
  metadata_settle_seconds: 0.0
media:
  allow_media_only: false
  block_on_spec_warning: false
spotify:
  headless: true
  processing_timeout_seconds: 60
  upload_timeout_seconds: 60
api:
  host: "127.0.0.1"
  port: 8799
  require_token: false
retry:
  max_attempts: 3
  base_delay_seconds: 0.2
  jitter: 0.0
""",
        encoding="utf-8",
    )
    cfg = load_config(path, root=runtime_root)
    cfg.ensure_directories()
    return cfg


@pytest.fixture
def database(config: AppConfig) -> Database:
    db = Database(config.database_path())
    yield db
    db.close()


@pytest.fixture
def show(config: AppConfig) -> ShowConfig:
    return config.shows[0]


@pytest.fixture
def incoming(config: AppConfig) -> Path:
    return config.show_incoming(config.shows[0])


# ------------------------------------------------------- integration wiring

@pytest.fixture
def spotify():
    """A real HTTP server serving the same pages as Spotify for Creators."""
    from tests.mock_spotify.server import MockSpotify

    server = MockSpotify().start()
    try:
        yield server
    finally:
        server.stop()


def _build_harness(config, database, spotify):
    """The production service wired to the mock, plus a thin test driver.

    Nothing in the application is replaced: the watcher, parser, validator,
    database, worker, publisher and archive manager are all the real ones.
    """
    from app.core.service import AutomationService
    from app.publishers.spotify.publisher import SpotifyPublisher
    from tests.fakes.fake_session import FakeSession
    from tests.harness import Harness

    config.spotify.base_url = spotify.base_url
    config.spotify.dashboard_url = f"{spotify.base_url}/pod/dashboard"
    config.spotify.login_url = f"{spotify.base_url}/login"
    config.spotify.episodes_url = f"{spotify.base_url}/pod/dashboard/episodes"
    config.spotify.upload_timeout_seconds = 20
    config.spotify.processing_timeout_seconds = 20

    sessions: list = []

    def factory():
        session = FakeSession(spotify.base_url)
        sessions.append(session)
        return session

    adapter = SpotifyPublisher(config, session_factory=factory, database=database)
    service = AutomationService(config, adapter=adapter, ensure_dirs=True)
    return Harness(config, database, spotify, service, adapter, sessions)


@pytest.fixture
def wired(config, database, spotify):
    return _build_harness(config, database, spotify)


@pytest.fixture
def harness(config, database, spotify):
    """The same production harness, named for readability in API/CLI tests."""
    return _build_harness(config, database, spotify)
