"""Pairing: matching a media file to its ``.txt`` by exact base filename.

The rule the user described: ``THOT_Der_Schluessel.mp4`` and
``THOT_Der_Schluessel.txt`` are one episode.  Pairing is therefore done on the
**exact base name** - never fuzzy, never "the only .txt in the folder", and
never case-insensitively on a filesystem that is case-sensitive (we compare
case-insensitively for *detection* but require the exact stem to match when
*claiming* a pair, so two files differing only in case cannot be crossed).

Ambiguity is always resolved by refusing, never by guessing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..core.models import MediaKind

__all__ = [
    "MediaCandidate",
    "MetadataCandidate",
    "EpisodePair",
    "scan_directory",
    "pair_directory",
    "find_pair",
    "base_name_of",
    "is_media_name",
    "is_metadata_name",
]


@dataclass(frozen=True)
class MediaCandidate:
    path: Path
    base_name: str
    extension: str
    size: int
    kind: MediaKind
    mtime_ns: int = 0

    @property
    def key(self) -> str:
        return self.base_name


@dataclass(frozen=True)
class MetadataCandidate:
    path: Path
    base_name: str
    extension: str
    size: int
    mtime_ns: int = 0

    @property
    def key(self) -> str:
        return self.base_name


@dataclass(frozen=True)
class EpisodePair:
    """A media file plus its matching metadata file."""

    media: MediaCandidate
    metadata: Optional[MetadataCandidate]
    show_id: str = ""
    job_type: str = "paired"

    @property
    def base_name(self) -> str:
        return self.media.base_name

    @property
    def media_path(self) -> Path:
        return self.media.path

    @property
    def metadata_path(self) -> Optional[Path]:
        return self.metadata.path if self.metadata else None

    def as_dict(self) -> dict:
        return {
            "show_id": self.show_id,
            "base_name": self.base_name,
            "job_type": self.job_type,
            "media": {
                "path": str(self.media.path),
                "name": self.media.path.name,
                "size": self.media.size,
                "kind": self.media.kind.value,
            },
            "metadata": (
                {"path": str(self.metadata.path), "name": self.metadata.path.name,
                 "size": self.metadata.size}
                if self.metadata else None
            ),
        }


def base_name_of(path: Path | str) -> str:
    """The base filename without directory or extension."""
    return Path(path).stem


def _fold(name: str) -> str:
    return name.casefold()


def is_media_name(name: str, policy=None) -> tuple[bool, MediaKind]:
    ext = Path(name).suffix.lower()
    if not ext:
        return False, MediaKind.UNKNOWN
    if policy is not None:
        audio = {e.lower() for e in policy.audio_extensions}
        video = {e.lower() for e in policy.video_extensions}
        if ext in audio:
            return True, MediaKind.AUDIO
        if ext in video:
            return True, MediaKind.VIDEO
        return False, MediaKind.UNKNOWN
    if ext in {".mp3", ".m4a", ".wav"}:
        return True, MediaKind.AUDIO
    if ext in {".mp4", ".mov", ".mpg", ".mpeg", ".m4v"}:
        return True, MediaKind.VIDEO
    return False, MediaKind.UNKNOWN


def is_metadata_name(name: str, policy=None) -> bool:
    ext = Path(name).suffix.lower()
    if policy is not None:
        return ext in {e.lower() for e in policy.metadata_extensions}
    return ext == ".txt"


def scan_directory(
    directory: Path | str,
    *,
    policy=None,
    ignore_suffixes: Sequence[str] = (),
) -> tuple[list[MediaCandidate], list[MetadataCandidate]]:
    """List candidate media and metadata files in *directory*.

    Sub-directories are ignored on purpose: the user drops files into
    ``incoming`` and nothing else.  Anything unreadable is skipped rather than
    raising, because a single locked file must not stop the whole watch.
    """
    root = Path(directory)
    media: list[MediaCandidate] = []
    metadata: list[MetadataCandidate] = []
    if not root.is_dir():
        return media, metadata
    ignore = {s.lower() for s in ignore_suffixes}
    try:
        entries = list(os.scandir(root))
    except (OSError, PermissionError) as exc:
        from ..security.redaction import redact_text
        raise OSError(f"Cannot list {root}: {redact_text(str(exc))}") from exc
    for entry in entries:
        name = entry.name
        try:
            if not entry.is_file(follow_symlinks=True):
                continue
        except OSError:
            continue
        suffix = Path(name).suffix.lower()
        if suffix in ignore or name.startswith((".", "~$")):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        stem = Path(name).stem
        # is_media_name returns (matched, kind); a non-empty tuple is always
        # truthy, so the flag has to be unpacked rather than tested directly.
        is_media, kind = is_media_name(name, policy)
        if is_media:
            media.append(MediaCandidate(
                path=root / name, base_name=stem, extension=suffix,
                size=st.st_size, kind=kind, mtime_ns=st.st_mtime_ns,
            ))
        elif is_metadata_name(name, policy):
            metadata.append(MetadataCandidate(
                path=root / name, base_name=stem, extension=suffix,
                size=st.st_size, mtime_ns=st.st_mtime_ns,
            ))
    media.sort(key=lambda c: (c.base_name.lower(), c.extension))
    metadata.sort(key=lambda c: c.base_name.lower())
    return media, metadata


def find_pair(
    media: MediaCandidate,
    metadata: Sequence[MetadataCandidate],
) -> tuple[Optional[MetadataCandidate], list[str]]:
    """Find the metadata file that belongs to *media*.

    Returns ``(match, problems)``.  Ambiguity produces ``problems`` and no
    match - we never guess which .txt the user meant.
    """
    problems: list[str] = []
    if not metadata:
        problems.append(
            f"No metadata file matches {media.path.name!r}. "
            f"Add a .txt file with the same name "
            f"(expected {media.base_name}.txt) before the episode can be published."
        )
        return None, problems
    folded = _fold(media.base_name)
    exact = [m for m in metadata if m.base_name == media.base_name]
    if len(exact) == 1:
        return exact[0], problems
    if len(exact) > 1:
        names = ", ".join(m.path.name for m in exact)
        problems.append(
            f"There are several metadata files with the same name ({names}). "
            f"Only one is allowed - rename the extras."
        )
        return None, problems
    # Case-insensitive fallback, but only when it is unambiguous.
    ci = [m for m in metadata if _fold(m.base_name) == folded]
    if len(ci) == 1:
        problems.append(
            f"Matched metadata file {ci[0].path.name!r} case-insensitively to "
            f"{media.path.name!r}. For reliability, make the file names identical."
        )
        return ci[0], problems
    if len(ci) > 1:
        problems.append(
            f"Several metadata files could match {media.path.name!r}: "
            + ", ".join(m.path.name for m in ci)
            + ". Rename them so the base names are unique."
        )
        return None, problems
    problems.append(
        f"No metadata file matches {media.path.name!r}. "
        f"Add a .txt file with the same name "
        f"(expected {media.base_name}.txt) before the episode can be published."
    )
    return None, problems


def pair_directory(
    directory: Path | str,
    *,
    policy=None,
    ignore_suffixes: Sequence[str] = (),
    allow_media_only: bool = False,
    show_id: str = "",
) -> tuple[list[EpisodePair], list[MediaCandidate], list[str]]:
    """Pair everything currently visible in *directory*.

    Returns ``(pairs, unpaired_media, problems)``.

    * ``pairs`` are ready for validation.
    * ``unpaired_media`` are media files still waiting for their .txt.
    * ``problems`` are human-readable warnings for the dashboard.
    """
    media, metadata = scan_directory(
        directory, policy=policy, ignore_suffixes=ignore_suffixes
    )
    pairs: list[EpisodePair] = []
    problems: list[str] = []
    unpaired: list[MediaCandidate] = []
    for candidate in media:
        match, pair_problems = find_pair(candidate, metadata)
        problems.extend(pair_problems)
        if match is not None:
            pairs.append(EpisodePair(
                media=candidate, metadata=match, show_id=show_id, job_type="paired"
            ))
        elif allow_media_only:
            pairs.append(EpisodePair(
                media=candidate, metadata=None, show_id=show_id, job_type="media_only"
            ))
        else:
            unpaired.append(candidate)
    return pairs, unpaired, problems

