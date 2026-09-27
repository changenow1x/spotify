"""Content hashing used for duplicate detection and idempotency keys.

Two different hashes are computed for every episode:

``media_hash``
    Identifies *the exact media file*.  Changing the audio changes this.

``content_hash``
    Identifies *the publication*.  ``sha256(media_hash + metadata_hash)``.
    Re-running the app on the same pair therefore always produces the same
    content hash, which is what the database unique index is built on.

Hashing mode
------------
``quick`` (default) hashes file size plus the first/middle/last 8 MiB.  This
costs ~24 MiB of reads instead of the full file, which matters when the user
drops a 6 GiB MP4.  ``full`` hashes every byte.  Both are SHA-256 and both are
collision-safe for the dedupe purpose here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO


__all__ = [
    "CHUNK_SIZE",
    "hash_bytes",
    "hash_text",
    "hash_file",
    "hash_path_quick",
    "content_hash",
    "HashResult",
]

CHUNK_SIZE = 1024 * 1024
_SAMPLE_BLOCK = 8 * 1024 * 1024


class HashResult:
    __slots__ = ("digest", "mode", "size", "mtime_ns", "elapsed_ms")

    def __init__(self, digest: str, mode: str, size: int, mtime_ns: int, elapsed_ms: float):
        self.digest = digest
        self.mode = mode
        self.size = size
        self.mtime_ns = mtime_ns
        self.elapsed_ms = elapsed_ms

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "mode": self.mode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"HashResult({self.digest[:12]}..., mode={self.mode}, size={self.size})"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Hash text after normalising line endings (so CRLF == LF)."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def hash_file(path: Path | str, *, chunk_size: int = CHUNK_SIZE) -> str:
    """Full SHA-256 of a file, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            h.update(block)
    return h.hexdigest()


def _sample_hash(fh: BinaryIO, size: int) -> str:
    """Hash size + first/middle/last blocks without reading the whole file."""
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    for offset in (0, max(0, (size - _SAMPLE_BLOCK) // 2), max(0, size - _SAMPLE_BLOCK)):
        fh.seek(offset)
        block = fh.read(_SAMPLE_BLOCK)
        h.update(offset.to_bytes(8, "big"))
        h.update(block)
    return h.hexdigest()


def hash_path_quick(path: Path | str) -> HashResult:
    import time

    start = time.perf_counter()
    p = Path(path)
    st = p.stat()
    with open(p, "rb") as fh:
        if st.st_size <= _SAMPLE_BLOCK:
            # Small files get a true full hash - it is cheap and exact.
            digest = hash_file(p)
        else:
            digest = _sample_hash(fh, st.st_size)
    return HashResult(
        digest=digest,
        mode="quick",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        elapsed_ms=(time.perf_counter() - start) * 1000,
    )


def hash_path(path: Path | str, *, mode: str = "quick") -> HashResult:
    import time

    start = time.perf_counter()
    p = Path(path)
    st = p.stat()
    if mode == "full" or st.st_size <= _SAMPLE_BLOCK:
        digest = hash_file(p)
        used = "full" if mode == "full" else "auto"
    else:
        digest = hash_path_quick(p).digest
        used = "quick"
    return HashResult(
        digest=digest,
        mode=used,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        elapsed_ms=(time.perf_counter() - start) * 1000,
    )


def content_hash(media_hash: str | None, metadata_hash: str | None) -> str:
    """Stable publication identity: same media + same metadata => same key."""
    h = hashlib.sha256()
    h.update((media_hash or "-").encode("ascii"))
    h.update(b"\x00")
    h.update((metadata_hash or "-").encode("ascii"))
    return h.hexdigest()


def safe_digest(value: str | None) -> str:
    """Abbreviate a digest for display in the dashboard/logs.

    This deliberately does *not* run the value through :func:`redact_text`:
    a content digest is computed by this app from the user's own file, so
    there is no secret to hide, and masking it would leave every job shown
    as "***REDACTED*".
    """
    if not value:
        return "-"
    return str(value)[:12]
