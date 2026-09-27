"""Archiving - moving a published pair out of ``incoming`` and into history.

Rules that this module enforces:

* **Never delete.**  Everything is moved; a copy is never made over an existing
  file without a numbered fallback, so an archive can never destroy a previous
  episode.
* **Never publish twice because of an archive failure.**  The job is already
  ``PUBLISHED`` before this runs, and a failure here leaves it there.
* **Safe paths only.**  Every destination is built with :func:`safe_join` from
  the archive root, so a filename containing ``..`` or a path separator cannot
  write outside the archive.
* The original filename is preserved exactly; the sanitised name is only used
  when the raw name is impossible on the filesystem.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from ..core.config import AppConfig, ShowConfig
from ..security.paths import (
    PathSafetyError,
    ensure_within,
    safe_join,
    sanitize_filename,
    unique_path,
)

log = logging.getLogger(__name__)

__all__ = ["ArchiveManager", "ArchiveResult"]


@dataclass
class ArchiveResult:
    ok: bool
    media_path: Optional[Path] = None
    metadata_path: Optional[Path] = None
    destination_dir: Optional[Path] = None
    error: str = ""
    moved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "media_path": str(self.media_path) if self.media_path else None,
            "metadata_path": str(self.metadata_path) if self.metadata_path else None,
            "destination_dir": str(self.destination_dir) if self.destination_dir else None,
            "error": self.error,
            "moved": self.moved,
        }


class ArchiveManager:
    def __init__(self, config: AppConfig):
        self.config = config

    # -- layout -------------------------------------------------------------
    def destination_dir(self, show: ShowConfig, *, when: datetime | None = None) -> Path:
        """``<archive>/<YYYY>/<MM>/`` by default (Spotify publishes in UTC)."""
        archive_root = self.config.show_path(show, "archive")
        when = when or datetime.now(timezone.utc)
        layout = self.config.archive.layout
        if layout == "flat":
            parts: Sequence[str] = ()
        elif layout == "year":
            parts = (f"{when.year:04d}",)
        else:  # "year-month"
            parts = (f"{when.year:04d}", f"{when.month:02d}")
        target = archive_root.joinpath(*parts) if parts else archive_root
        try:
            ensure_within(archive_root, target)
        except PathSafetyError:
            log.error("Archive layout %r produced an unsafe path; using the archive root.",
                      layout)
            target = archive_root
        return target

    # -- the operation -------------------------------------------------------
    def archive(
        self,
        show: ShowConfig,
        media_path: Path | str,
        metadata_path: Path | str | None = None,
        *,
        when: datetime | None = None,
        extra_files: Sequence[Path | str] = (),
    ) -> ArchiveResult:
        """Move the media (and its metadata) into the archive tree."""
        media = Path(media_path)
        metadata = Path(metadata_path) if metadata_path else None
        if not media.exists():
            return ArchiveResult(False, error=f"The media file no longer exists: {media}")

        dest_dir = self.destination_dir(show, when=when)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ArchiveResult(False, error=f"Could not create {dest_dir}: {exc}")

        moved: list[str] = []
        media_dest: Optional[Path] = None
        meta_dest: Optional[Path] = None

        for source, is_metadata in [(media, False)] + ([(metadata, True)] if metadata else []):
            if source is None or not source.exists():
                continue
            name = source.name if self.config.archive.keep_original_names else \
                sanitize_filename(source.name)
            name = sanitize_filename(name)
            try:
                target = safe_join(dest_dir, name)
            except PathSafetyError as exc:
                return ArchiveResult(False, moved=moved, error=str(exc))
            try:
                if target.exists() and not _same(target, source):
                    target = unique_path(target)
                _move(source, target)
            except OSError as exc:
                return ArchiveResult(
                    False, media_path=media_dest, metadata_path=meta_dest,
                    destination_dir=dest_dir, moved=moved,
                    error=f"Could not move {source.name}: {exc.strerror or exc}",
                )
            moved.append(source.name)
            if is_metadata:
                meta_dest = target
            else:
                media_dest = target

        for extra in extra_files:
            source = Path(extra)
            if not source.exists():
                continue
            try:
                target = unique_path(safe_join(dest_dir, sanitize_filename(source.name)))
                _move(source, target)
                moved.append(source.name)
            except (OSError, PathSafetyError) as exc:
                log.warning("Could not archive the extra file %s: %s", source.name, exc)

        return ArchiveResult(
            ok=True, media_path=media_dest, metadata_path=meta_dest,
            destination_dir=dest_dir, moved=moved,
        )

    def archive_job_files(self, show: ShowConfig, job) -> ArchiveResult:
        """Convenience wrapper for a :class:`JobRow`."""
        return self.archive(
            show,
            job["media_path"],
            job.get("metadata_path"),
        )

    # -- housekeeping --------------------------------------------------------
    def move_to_failed(self, show: ShowConfig, media_path: Path | str,
                       metadata_path: Path | str | None = None) -> ArchiveResult:
        """Move an unusable pair out of ``incoming`` so the watcher stops seeing it."""
        media = Path(media_path)
        metadata = Path(metadata_path) if metadata_path else None
        target_dir = self.config.show_path(show, "failed")
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ArchiveResult(False, error=f"Could not create {target_dir}: {exc}")
        moved: list[str] = []
        media_dest = None
        try:
            target = unique_path(safe_join(target_dir, sanitize_filename(media.name)))
            _move(media, target)
            moved.append(media.name)
            media_dest = target
        except (OSError, PathSafetyError) as exc:
            return ArchiveResult(False, error=str(exc), moved=moved)
        meta_dest = None
        if metadata and metadata.exists():
            try:
                target = unique_path(safe_join(target_dir, sanitize_filename(metadata.name)))
                _move(metadata, target)
                moved.append(metadata.name)
                meta_dest = target
            except (OSError, PathSafetyError) as exc:
                log.warning("Could not move the failed metadata file: %s", exc)
        return ArchiveResult(
            ok=True, media_path=media_dest, metadata_path=meta_dest,
            destination_dir=target_dir, moved=moved,
        )

    def list_archived(self, show: ShowConfig, limit: int = 200) -> list[dict]:
        root = self.config.show_path(show, "archive")
        if not root.exists():
            return []
        out: list[dict] = []
        for path in sorted(root.rglob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0,
                           reverse=True):
            if path.is_file():
                try:
                    st = path.stat()
                except OSError:  # pragma: no cover
                    continue
                out.append({
                    "name": path.name,
                    "path": str(path),
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
            if len(out) >= limit:
                break
        return out

    def archive_size(self, show: ShowConfig) -> int:
        root = self.config.show_path(show, "archive")
        if not root.exists():
            return 0
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:  # pragma: no cover
                continue
        return total


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return a.resolve(strict=False) == b.resolve(strict=False)


def _move(source: Path, target: Path) -> None:
    """Move a file, trying rename first, then copy+unlink across devices.

    Windows raises ``PermissionError`` when a file is momentarily locked, so a
    short retry is worthwhile; a cross-device move (D: -> C:) needs copy+unlink.
    """
    import time

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
        except OSError as exc:
            if exc.errno in (18, 17):  # EXDEV on Linux, cross-device on some Windows setups
                break
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    try:
        shutil.copy2(source, target)
        source.unlink()
        return
    except OSError as exc:
        raise OSError(
            getattr(exc, "strerror", None) or str(exc)
        ) from (last_error or exc)
