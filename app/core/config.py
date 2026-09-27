"""Application configuration.

Everything non-secret lives in ``config/config.yaml``.  Adding a show, changing
a folder, or flipping a policy is a config edit - never a code edit.

Design rules:
* Paths in config may be absolute (``D:\\PodcastAutomation\\shows\\Foo``) or
  relative to the runtime root.
* Unknown top-level keys are rejected with a helpful message (so a typo does
  not silently disable a feature) - but ``#`` comments and forward-compatible
  keys under ``future`` are tolerated.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import resolve_timezone

APP_NAME = "PodcastAutomation"
CONFIG_FILENAME = "config.yaml"

#: Sub-directories created for every show.
SHOW_SUBDIRS = ("incoming", "processing", "failed", "archive", "config")


class ConfigError(ValueError):
    """Raised for any invalid or unusable configuration."""


def default_root() -> Path:
    """Where the application keeps shows, logs and data.

    Windows (the primary target) uses ``C:\\PodcastAutomation`` unless the
    ``PODCAST_AUTOMATION_ROOT`` environment variable overrides it.  On other
    operating systems a repo-local ``runtime/`` directory is used so the
    project is runnable for development and testing.
    """
    env = os.environ.get("PODCAST_AUTOMATION_ROOT")
    if env:
        return Path(env).expanduser()
    if platform.system() == "Windows":
        return Path("C:/PodcastAutomation")
    return Path(__file__).resolve().parents[2] / "runtime"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=5, ge=1, le=50)
    base_delay_seconds: float = Field(default=20.0, gt=0, le=3600)
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    max_delay_seconds: float = Field(default=900.0, ge=1, le=86400)
    jitter: float = Field(default=0.20, ge=0.0, le=1.0)

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        import random

        attempt = max(1, attempt)
        raw = self.base_delay_seconds * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_delay_seconds)
        if self.jitter <= 0:
            return capped
        return max(1.0, capped * (1.0 - self.jitter * random.random()))


class MediaPolicy(StrictModel):
    """Which extensions we accept and how hard we fail on spec deviations."""

    audio_extensions: list[str] = Field(default_factory=lambda: [".mp3", ".m4a", ".wav"])
    video_extensions: list[str] = Field(default_factory=lambda: [".mp4", ".mov", ".mpg", ".mpeg", ".m4v"])
    metadata_extensions: list[str] = Field(default_factory=lambda: [".txt"])

    #: If true, a *warning*-level spec deviation still blocks publication until
    #: the user explicitly forces it.  Safe default: true.
    block_on_spec_warning: bool = True
    #: Allow a media file with no .txt to be published using filename-derived
    #: metadata.  Safe default: false.
    allow_media_only: bool = False
    #: Reject files larger than this (0 = no limit).  Spotify's documented
    #: compatible ceiling is 60 GB; the recommendation is <10 GB.
    max_file_size_bytes: int = Field(default=0, ge=0)
    warn_file_size_bytes: int = Field(default=10 * 1024 ** 3, ge=0)
    #: Spotify's hard documented maximum episode duration.
    max_duration_seconds: int = Field(default=12 * 3600, ge=0)
    warn_duration_seconds: int = Field(default=4 * 3600, ge=0)
    #: Minimum audio bitrate recommended by Spotify.  A warning, not a limit.
    min_audio_bitrate: int = Field(default=128_000, ge=0)
    #: For video, Spotify's page recommends 192 kbps or higher.  Still only a
    #: recommendation - the episode publishes either way.
    min_video_audio_bitrate: int = Field(default=192_000, ge=0)
    ffprobe_path: str = ""
    ffmpeg_path: str = ""

    def all_media_extensions(self) -> list[str]:
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}"
                for e in (*self.audio_extensions, *self.video_extensions)}
        return sorted(exts)


class ShowDefaults(StrictModel):
    """Show-level metadata defaults.  Episode metadata always wins."""

    explicit: Optional[bool] = None
    publish: Optional[str] = None
    timezone: Optional[str] = None
    language: Optional[str] = None
    author: Optional[str] = None
    season: Optional[int] = Field(default=None, ge=0)
    publish_time: Optional[str] = None
    publish_date: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None

    def as_prefix_pairs(self) -> list[tuple[str, str]]:
        """Non-``None`` defaults rendered as ``DEFAULT_<KEY>=<value>`` pairs."""
        out: list[tuple[str, str]] = []
        for key, value in self.__dict__.items():
            if value is None:
                continue
            if isinstance(value, bool):
                out.append((f"DEFAULT_{key.upper()}", "true" if value else "false"))
            elif isinstance(value, list):
                out.append((f"DEFAULT_{key.upper()}", ", ".join(str(v) for v in value)))
            else:
                out.append((f"DEFAULT_{key.upper()}", str(value)))
        return out


class ShowConfig(StrictModel):
    """One show.  ``incoming`` is the only folder the user really touches."""

    name: str = Field(min_length=1)
    #: Folder name under ``<root>/shows``.  Defaults to a slug of ``name``.
    id: Optional[str] = None
    enabled: bool = True

    incoming: str = ""
    processing: str = ""
    failed: str = ""
    archive: str = ""

    #: Spotify show identifier.  Either the numeric show id shown in the
    #: Spotify for Creators URL, or the show's public handle/name.  Optional:
    #: when empty, the publisher picks the only show on the account and refuses
    #: to guess if there is more than one.
    spotify_show_id: str = ""
    spotify_show_name: str = ""

    timezone: str = "Europe/Berlin"
    defaults: ShowDefaults = Field(default_factory=ShowDefaults)

    def resolved_id(self) -> str:
        """The directory-safe identifier for this show.

        The id comes from ``config.yaml`` and ends up as a *folder name*, so a
        value like ``../../etc`` would put the show's incoming, archive and
        failed folders outside the application root.  Anything that is not a
        plain, readable slug is therefore slugified rather than trusted.
        """
        from .models import Chapter  # noqa: F401  (keep import graph explicit)
        import re

        raw = self.id or self.name
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(raw)).strip("-").lower()
        slug = slug.strip(".")
        return slug or "show"

    def all_subdirs(self) -> list[str]:
        return list(SHOW_SUBDIRS)


class WatcherConfig(StrictModel):
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    #: A file must keep this size/mtime for this long before it is considered
    #: fully copied.  Raise for very large files over slow links.
    stable_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    #: Additional consecutive stable observations required.
    stable_checks: int = Field(default=2, ge=1, le=100)
    #: Give up waiting for a stable file after this long (then report).
    stability_timeout_seconds: float = Field(default=7200.0, gt=0)
    #: Wait for a file lock to clear (Windows virus scanners, Explorer thumbs).
    lock_retry_seconds: float = Field(default=30.0, gt=0)
    #: Ignore files smaller than this (stray .txt artefacts, Office lock files).
    min_media_bytes: int = Field(default=1024, ge=0)
    #: Max size of a metadata .txt we will read.
    max_metadata_bytes: int = Field(default=1024 * 1024, ge=1024)
    #: Ignore our own working files.
    ignore_suffixes: list[str] = Field(
        default_factory=lambda: [".tmp", ".part", ".crdownload", ".download",
                                 ".!qb", ".opdownload", ".partial", ".filepart", ".lrc"]
    )
    #: Wait this long after a metadata file appears before pairing, in case the
    #: user is still typing it.
    metadata_settle_seconds: float = Field(default=2.0, ge=0, le=600)
    #: Startup rescan of every incoming folder (covers events missed while down).
    rescan_on_start: bool = True


class SpotifyConfig(StrictModel):
    base_url: str = "https://creators.spotify.com"
    login_url: str = "https://creators.spotify.com/pod/login"
    dashboard_url: str = "https://creators.spotify.com/pod/dashboard"
    episodes_url: str = "https://creators.spotify.com/pod/dashboard/episodes"

    #: Run the visible browser window (needed for first-time login).
    headless: bool = False
    #: Slow operations down so a human can watch/verify.  0 = fastest.
    slow_mo_ms: int = Field(default=0, ge=0, le=10000)
    browser: str = Field(default="chromium", pattern="^(chromium|chrome|msedge|firefox)$")
    default_timeout_ms: int = Field(default=45_000, ge=5_000, le=600_000)
    navigation_timeout_ms: int = Field(default=120_000, ge=10_000, le=900_000)
    #: Total time budget for the media upload itself (6 GiB over slow DSL...).
    upload_timeout_seconds: int = Field(default=14_400, ge=60, le=86_400)
    #: How long to wait for Spotify to finish transcoding/processing.
    processing_timeout_seconds: int = Field(default=3_600, ge=60, le=86_400)
    locale: str = "de-DE"
    viewport_width: int = 1280
    viewport_height: int = 900
    #: Save a DOM snapshot + screenshot on any automation error (diagnostics).
    capture_diagnostics: bool = True
    diagnostics_dir: str = "diagnostics"
    #: Keep a screenshot of the final published episode page.
    capture_success_screenshots: bool = False
    #: Reuse the persistent Chromium profile at <root>/data/browser-profile.
    persistent_profile: bool = True
    #: Optional explicit profile directory (else default under data/).
    profile_dir: str = ""


class ArchiveConfig(StrictModel):
    #: archive/<YYYY>/<MM>/<original filename>
    layout: str = "year-month"
    keep_original_names: bool = True
    #: Copy metadata alongside the media (always true in practice).
    include_metadata: bool = True
    #: Also keep a copy of the exact .txt that was published.
    snapshot_metadata: bool = True
    #: Retry archiving this many times before giving up (job stays PUBLISHED).
    max_attempts: int = Field(default=3, ge=1, le=20)


class ApiConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    #: Bind to all interfaces so the browser preview/proxy can reach it.
    open_browser_on_start: bool = False
    enable: bool = True
    #: CSRF-ish shared token for mutating dashboard requests.
    require_token: bool = True


class SecurityConfig(StrictModel):
    #: Where the encrypted browser-session blob / tokens live.
    secret_store: str = "auto"        # auto | keyring | file
    secret_file_name: str = "secrets.enc"
    encrypt_secrets: bool = True
    #: Refuse these metadata values outright (defence in depth).
    max_filename_length: int = 200
    max_metadata_length: int = 200_000
    max_title_length: int = 400
    max_description_length: int = 20_000
    max_chapters: int = Field(default=200, ge=1, le=1000)
    #: Minimum gap between chapters (Spotify documents 30 s).
    min_chapter_gap_seconds: int = Field(default=30, ge=0, le=3600)
    min_chapters: int = Field(default=3, ge=0, le=50)


class LoggingConfig(StrictModel):
    level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    #: app.log + rotating copies.
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=10_000)
    backup_count: int = Field(default=10, ge=1, le=100)
    console_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    #: Human-readable activity log that drives the dashboard timeline.
    activity_file: str = "activity.log"
    retain_days: int = Field(default=90, ge=1, le=3650)


class AppConfig(StrictModel):
    """The whole application configuration."""

    root: str = ""
    #: hash_mode: quick | full
    hash_mode: str = Field(default="quick", pattern="^(quick|full)$")
    #: Absolute path to this config file (filled in by the loader).
    source_path: str = ""

    shows: list[ShowConfig] = Field(default_factory=list)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    media: MediaPolicy = Field(default_factory=MediaPolicy)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    #: Number of episodes processed at once.  Spotify's UI is single-account
    #: and single-tab, so >1 is only safe with careful session reuse.
    max_concurrent_jobs: int = Field(default=1, ge=1, le=8)
    #: Do not create a job at all when the queue is paused.
    pause_queue: bool = False

    @model_validator(mode="after")
    def _check_shows(self) -> "AppConfig":
        seen: set[str] = set()
        for show in self.shows:
            sid = show.resolved_id()
            if sid in seen:
                raise ConfigError(
                    f"Two shows resolve to the id {sid!r}. Give one of them an "
                    f"explicit `id:` so they do not collide."
                )
            seen.add(sid)
            try:
                resolve_timezone(show.timezone)
            except ValueError as exc:
                raise ConfigError(f"Show {show.name!r}: {exc}") from exc
        return self

    # -- derived paths ----------------------------------------------------
    def root_path(self) -> Path:
        return Path(self.root).expanduser() if self.root else default_root()

    def shows_dir(self) -> Path:
        return self.root_path() / "shows"

    def logs_dir(self) -> Path:
        return self.root_path() / "logs"

    def data_dir(self) -> Path:
        return self.root_path() / "data"

    def config_dir(self) -> Path:
        return self.root_path() / "config"

    def database_path(self) -> Path:
        return self.data_dir() / "podcast.db"

    def secrets_path(self) -> Path:
        return self.data_dir() / self.security.secret_file_name

    def profile_dir(self) -> Path:
        if self.spotify.profile_dir:
            p = Path(self.spotify.profile_dir).expanduser()
            return p if p.is_absolute() else self.root_path() / p
        return self.data_dir() / "browser-profile"

    def diagnostics_dir(self) -> Path:
        return self.logs_dir() / self.spotify.diagnostics_dir

    def show_dir(self, show: ShowConfig) -> Path:
        return self.shows_dir() / show.resolved_id()

    def show_incoming(self, show: ShowConfig) -> Path:
        if show.incoming:
            p = Path(show.incoming).expanduser()
            return p if p.is_absolute() else self.root_path() / p
        return self.show_dir(show) / "incoming"

    def show_path(self, show: ShowConfig, kind: str) -> Path:
        attr = getattr(show, kind, "") or ""
        if attr:
            p = Path(attr).expanduser()
            return p if p.is_absolute() else self.root_path() / p
        return self.show_dir(show) / kind

    def get_show(self, show_id: str) -> Optional[ShowConfig]:
        for show in self.shows:
            if show.resolved_id() == show_id:
                return show
        return None

    def enabled_shows(self) -> list[ShowConfig]:
        return [s for s in self.shows if s.enabled]

    def ensure_directories(self) -> list[Path]:
        """Create the whole folder tree.  Returns every directory created."""
        created: list[Path] = []

        def _mk(p: Path) -> None:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                created.append(p)

        _mk(self.root_path())
        _mk(self.shows_dir())
        _mk(self.logs_dir())
        _mk(self.data_dir())
        _mk(self.config_dir())
        _mk(self.diagnostics_dir())
        for show in self.shows:
            for sub in show.all_subdirs():
                _mk(self.show_path(show, sub))
            if not show.incoming:
                _mk(self.show_incoming(show))
        return created

    def as_dict(self) -> dict[str, Any]:
        from pydantic import BaseModel as _BM
        return _BM.model_dump(self) if hasattr(_BM, "model_dump") else self.model_dump()  # type: ignore[attr-defined]


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env_overrides(data: dict) -> dict:
    """Allow a few env overrides for container/automation use."""
    if "PODCAST_ROOT" in os.environ:
        data["root"] = os.environ["PODCAST_ROOT"]
    if "PODCAST_HEADLESS" in os.environ:
        data.setdefault("spotify", {})["headless"] = os.environ["PODCAST_HEADLESS"].lower() in {"1", "true", "yes"}
    if "PODCAST_PORT" in os.environ:
        data.setdefault("api", {})["port"] = int(os.environ["PODCAST_PORT"])
    return data


def find_config_file(root: Path | None = None) -> Path:
    """Locate ``config.yaml``: explicit root first, then repo-local, then cwd."""
    candidates: list[Path] = []
    if root:
        candidates += [root / "config" / CONFIG_FILENAME, root / CONFIG_FILENAME]
    here = Path(__file__).resolve().parents[2]
    candidates += [
        here / "runtime" / "config" / CONFIG_FILENAME,
        here / CONFIG_FILENAME,
        Path.cwd() / "config" / CONFIG_FILENAME,
        Path.cwd() / CONFIG_FILENAME,
    ]
    base = default_root()
    candidates += [base / "config" / CONFIG_FILENAME, base / CONFIG_FILENAME]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def load_config(
    path: Path | str | None = None,
    *,
    root: Path | str | None = None,
    overrides: dict | None = None,
) -> AppConfig:
    """Load and validate configuration from YAML.

    Raises :class:`ConfigError` with an actionable message on any problem.
    """
    import yaml

    if path is None:
        path = find_config_file(Path(root) if root else None)
    path = Path(path).expanduser()

    data: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read config {path}: {exc}") from exc
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
        data = loaded

    data = _apply_env_overrides(data)
    if root is not None:
        data["root"] = str(root)
    if overrides:
        data = _deep_merge(data, overrides)

    data.setdefault("root", str(default_root()))
    data["source_path"] = str(path)

    try:
        cfg = AppConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(format_config_error(exc, path)) from exc

    if not cfg.shows:
        raise ConfigError(
            f"No shows are configured in {path}.\n"
            f"Add at least one show, for example:\n\n"
            f"shows:\n"
            f"  - name: My Podcast\n"
            f"    spotify_show_id: ''\n"
            f"    timezone: Europe/Berlin\n"
        )
    return cfg


def format_config_error(exc: Exception, path: Path) -> str:
    """Turn a pydantic error into something a non-programmer can act on."""
    lines = [f"Configuration problem in {path}:"]
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for err in errors()[:20]:
            loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
            msg = err.get("msg", "invalid value")
            hint = ""
            if "extra" in str(err.get("type", "")):
                hint = " (unknown key - remove it or check the spelling)"
            lines.append(f"  - {loc}: {msg}{hint}")
    else:  # pragma: no cover
        lines.append(f"  - {exc}")
    return "\n".join(lines)


def dump_config(cfg: AppConfig, path: Path | str) -> Path:
    """Write the config back out (used by ``--setup`` and the dashboard)."""
    import yaml

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.model_dump(exclude={"root", "source_path"}, mode="json")
    payload.pop("source_path", None)
    header = (
        "# PodcastAutomation configuration\n"
        "# Add a show by appending another entry under `shows:` - no code changes.\n"
        "# Full reference: docs/CONFIGURATION.md\n"
    )
    path.write_text(header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


CONFIG_TEMPLATE: dict[str, Any] = {
    "root": "",
    "hash_mode": "quick",
    "max_concurrent_jobs": 1,
    "pause_queue": False,
    "shows": [
        {
            "name": "My Podcast",
            "enabled": True,
            "timezone": "Europe/Berlin",
            "spotify_show_id": "",
            "spotify_show_name": "",
            "defaults": {
                "explicit": False,
                "publish": "NOW",
                "timezone": "Europe/Berlin",
                "language": "de",
            },
        }
    ],
    "watcher": {
        "stable_seconds": 5.0,
        "poll_interval_seconds": 1.0,
    },
    "media": {
        "allow_media_only": False,
        "block_on_spec_warning": True,
    },
    "spotify": {
        "headless": False,
    },
    "api": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "retry": {
        "max_attempts": 5,
        "base_delay_seconds": 20.0,
    },
}
