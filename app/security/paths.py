"""Safe filesystem path helpers.

Every path that originates from user data (a dropped filename, a metadata
value, a config entry) must pass through this module.  The rules are:

* never build a path by string concatenation from untrusted input
* never allow ``..`` traversal or absolute-path escape out of a sandbox root
* never allow Windows reserved device names (CON, NUL, PRN, ...)
* never allow characters that are illegal on Windows filesystems
* resolve to an absolute, symlink-free path before use

The original user-supplied name is *always* preserved in the database and in
logs; sanitisation only affects what is written to disk.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePath

__all__ = [
    "PathSafetyError",
    "is_within",
    "safe_join",
    "sanitize_filename",
    "sanitize_stem",
    "resolve_within",
    "ensure_within",
    "unique_path",
]

# Characters that are illegal in Windows filenames.  We are permissive on
# purpose: the app runs on Windows first, so we use the Windows denylist
# everywhere.  This means a Linux user cannot use ':' in a filename, which is
# an acceptable trade-off for predictable cross-platform behaviour.
_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# Reserved DOS device names.  Applies with or without extension.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Trailing dots/spaces are silently stripped by Windows, which would make the
# archive filename differ from what we recorded.  We strip them ourselves.
_TRAILING_JUNK = re.compile(r"[ .]+$")

MAX_FILENAME_LEN = 180
MAX_STEM_LEN = 150

#: Windows drive-qualified (``C:\x``) and UNC (``\\server\share``) prefixes.
#: Checked explicitly so a path that is harmless on Linux is still refused when
#: the same configuration file is used on the Windows machine it targets.
_DRIVE_OR_UNC = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")


class PathSafetyError(ValueError):
    """Raised when a path would escape its sandbox root or is unusable."""


def is_within(root: Path | str, candidate: Path | str) -> bool:
    """Return True if *candidate* resolves to a location inside *root*."""
    try:
        root_r = Path(root).expanduser().resolve(strict=False)
        cand_r = Path(candidate).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as exc:  # pragma: no cover
        raise PathSafetyError(f"Cannot resolve path: {exc}") from exc
    if cand_r == root_r:
        return True
    return root_r in cand_r.parents


def safe_join(root: Path | str, *parts: str) -> Path:
    """Join untrusted *parts* onto *root*, guaranteeing containment.

    Raises :class:`PathSafetyError` if any part is absolute, contains a drive
    letter, or would otherwise escape *root*.
    """
    root_p = Path(root).expanduser()
    for raw in parts:
        if raw is None:
            raise PathSafetyError("Path component must not be None")
        text = str(raw)
        if not text:
            raise PathSafetyError("Path component must not be empty")
        if "\x00" in text:
            raise PathSafetyError("Path component contains a NUL byte")
        # Reject anything that is itself absolute or drive-qualified.
        pure = PurePath(text)
        if (pure.is_absolute() or pure.drive or text.startswith(("/", "\\"))
                or _DRIVE_OR_UNC.match(text)):
            raise PathSafetyError(
                f"Absolute paths are not allowed here: {text!r}"
            )
        if any(part == ".." for part in pure.parts):
            raise PathSafetyError(
                f"Path traversal ('..') is not allowed: {text!r}"
            )
    joined = root_p.joinpath(*[str(p) for p in parts])
    if not is_within(root_p, joined):
        raise PathSafetyError(
            f"Refusing to use {joined!s}: it escapes {root_p!s}"
        )
    return joined


def resolve_within(root: Path | str, candidate: Path | str) -> Path:
    """Return the absolute resolved *candidate*, requiring it to be in *root*."""
    cand = Path(candidate).expanduser()
    if not is_within(root, cand):
        raise PathSafetyError(
            f"{cand!s} is outside the permitted root {Path(root)!s}"
        )
    return cand.resolve(strict=False)


def ensure_within(root: Path | str, candidate: Path | str) -> Path:
    """Assert containment and return the candidate unchanged (no resolution).

    Useful on Windows where ``resolve()`` can be slow on network shares.
    """
    if not is_within(root, candidate):
        raise PathSafetyError(
            f"{Path(candidate)!s} is outside the permitted root {Path(root)!s}"
        )
    return Path(candidate)


def sanitize_filename(name: str, *, fallback: str = "unnamed", max_len: int = MAX_FILENAME_LEN) -> str:
    """Return a filesystem-safe *name* (no directories) preserving readability.

    Unicode (German umlauts, Greek letters, ...) is preserved in NFC form; only
    genuinely illegal characters are replaced.
    """
    if name is None:
        name = ""
    # Keep only the last path component - a metadata file must never be able to
    # inject a directory.
    base = PurePath(str(name).replace("\\", "/")).name
    base = unicodedata.normalize("NFC", base)
    base = _ILLEGAL_CHARS.sub("_", base)
    base = _TRAILING_JUNK.sub("", base)
    base = base.strip()
    if not base or base in {".", ".."}:
        return fallback
    # A name that begins with dots can be mistaken for a relative path by
    # later tools (shells, sync clients, our own logs).  Keep the extension
    # visible but drop the leading dot run.
    if base.startswith("."):
        stem, dot, ext = base.lstrip(".").rpartition(".")
        base = f".{stem}.{ext}" if (dot and stem) else f".{base.lstrip('.')}"
    stem, dot, ext = base.rpartition(".")
    reserved_stem = stem if dot else base
    if reserved_stem.upper() in _RESERVED_NAMES:
        # Windows silently redirects "nul.txt" to a device; disambiguate it.
        base = f"{stem}_.{ext}" if dot else f"{base}_"
    if len(base) > max_len:
        # Preserve the extension when truncating.
        stem2, dot2, ext2 = base.rpartition(".")
        if dot2 and len(ext2) <= 12:
            keep = max(1, max_len - len(ext2) - 1)
            base = f"{stem2[:keep]}.{ext2}"
        else:
            base = base[:max_len]
    return base


def sanitize_stem(name: str, *, fallback: str = "episode", max_len: int = MAX_STEM_LEN) -> str:
    """Sanitize a *stem* (filename without extension), preserving Unicode."""
    return sanitize_filename(name, fallback=fallback, max_len=max_len)


def unique_path(path: Path) -> Path:
    """Return *path* or ``name (2).ext`` / ``name (3).ext`` if it exists."""
    if not path.exists():
        return path
    stem, dot, ext = path.name.rpartition(".")
    if not dot:
        stem, ext = path.name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}){('.' + ext) if ext else ''}"
        full = path.with_name(candidate)
        if not full.exists():
            return full
        n += 1


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def same_file(a: Path | str, b: Path | str) -> bool:
    """Best-effort identity check that works even when a file is being written."""
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        try:
            return Path(a).resolve(strict=False) == Path(b).resolve(strict=False)
        except (OSError, ValueError):  # pragma: no cover
            return False


#: Hosts the dashboard is allowed to hand to the operating system.
SPOTIFY_LINK_HOSTS = frozenset({"open.spotify.com", "spotify.com", "www.spotify.com"})


def is_spotify_url(url: str) -> bool:
    """True only for an https URL on a real Spotify host.

    Rejects other schemes, embedded credentials, control characters (which
    would let a second URL hide behind a valid prefix) and look-alike hosts
    such as ``open.spotify.com.evil.example``.
    """
    from urllib.parse import urlsplit

    if not url or len(url) > 2000:
        return False
    if any(ch in url for ch in "\r\n\t\x00") or any(ord(ch) < 32 for ch in url):
        return False
    if any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    if parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    return host in SPOTIFY_LINK_HOSTS
