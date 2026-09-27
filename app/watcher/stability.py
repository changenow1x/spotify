"""File-stability detection.

The problem this solves: a user drops a 6 GB MP4 into the incoming folder and
Explorer keeps writing to it for several minutes.  If we probe or upload it
immediately we will read a truncated file and Spotify will reject it.

A file is considered *stable* only when **all** of the following hold:

1. its size and modification time are unchanged for ``stable_seconds``;
2. that observation repeated ``stable_checks`` consecutive times;
3. the file can actually be opened for reading and a real read succeeds
   (this catches Windows file locks from antivirus, Explorer thumbnails,
   OneDrive and file-copy utilities);
4. the whole wait did not exceed ``stability_timeout_seconds``.

Nothing here uses a fixed ``sleep(5)`` guess: every wait is a real
observation, and the result is reported with the reason it was rejected.
"""

from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "StabilityTracker",
    "FileStability",
    "is_file_locked",
    "readable",
    "wait_until_stable",
    "StabilityTimeout",
]


class StabilityTimeout(TimeoutError):
    def __init__(self, path: Path, seconds: float, last: "FileStability"):
        self.path = path
        self.seconds = seconds
        self.last = last
        super().__init__(
            f"{Path(path).name} was still changing after {seconds / 60:.0f} minutes. "
            f"It is currently {last.size} bytes. The copy may have stalled - check "
            f"the file and drop it again once it is fully copied."
        )


@dataclass
class FileStability:
    path: Path
    size: int = -1
    mtime_ns: int = -1
    stable_since: Optional[float] = None
    observations: int = 0
    consecutive: int = 0
    locked: bool = False
    read_error: str = ""
    first_seen: float = field(default_factory=time.monotonic)

    @property
    def exists(self) -> bool:
        return self.size >= 0

    def as_dict(self) -> dict:
        return {
            "name": self.path.name,
            "size": self.size,
            "stable_for": round(time.monotonic() - (self.stable_since or self.first_seen), 2),
            "observations": self.observations,
            "locked": self.locked,
            "read_error": self.read_error,
        }


def is_file_locked(path: Path | str) -> bool:
    """True when the file cannot be opened for reading right now.

    On Windows this is how a locked file presents itself; on POSIX we also
    check the advisory lock, which some copy tools leave behind.
    """
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            fh.read(1)
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ETXTBSY, errno.EBUSY):
            return True
        if exc.errno == errno.ENOENT:
            return False
        return False
    if os.name == "nt":
        return False
    try:
        import fcntl

        with open(p, "rb") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):  # pragma: no cover
        pass
    return False


def readable(path: Path | str) -> tuple[bool, str]:
    """Open the file and read a chunk.  Returns ``(ok, error_message)``."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(64 * 1024)
            if chunk == b"" and Path(path).stat().st_size > 0:
                return False, "the file opened but no data could be read from it"
        return True, ""
    except PermissionError:
        return False, "another program is currently writing to the file"
    except OSError as exc:
        return False, f"the file cannot be read ({exc.strerror or exc})"


class StabilityTracker:
    """Incremental stability state for many files at once.

    ``poll`` is called from the watcher loop; it never blocks.  A file becomes
    stable when :meth:`poll` has seen ``consecutive`` identical observations
    separated by at least ``stable_seconds`` and the file is readable.
    """

    def __init__(
        self,
        *,
        stable_seconds: float = 5.0,
        stable_checks: int = 2,
        poll_interval: float = 1.0,
        timeout: float = 7200.0,
    ):
        self.stable_seconds = max(0.0, float(stable_seconds))
        self.stable_checks = max(1, int(stable_checks))
        self.poll_interval = max(0.05, float(poll_interval))
        self.timeout = max(1.0, float(timeout))
        self._files: dict[Path, FileStability] = {}

    def track(self, path: Path | str) -> FileStability:
        p = Path(path)
        state = self._files.get(p)
        if state is None:
            state = FileStability(path=p)
            self._files[p] = state
        return state

    def forget(self, path: Path | str) -> None:
        self._files.pop(Path(path), None)

    def tracked(self) -> list[FileStability]:
        return list(self._files.values())

    def poll(self, path: Path | str) -> FileStability:
        """Observe *path* once.  Returns its updated state."""
        p = Path(path)
        state = self.track(p)
        state.observations += 1
        try:
            st = p.stat()
        except FileNotFoundError:
            state.size = -1
            state.mtime_ns = -1
            state.locked = False
            state.read_error = ""
            return state
        except OSError as exc:
            state.read_error = f"cannot stat the file: {exc.strerror or exc}"
            return state

        if st.st_size == state.size and st.st_mtime_ns == state.mtime_ns:
            state.consecutive += 1
        else:
            state.size = st.st_size
            state.mtime_ns = st.st_mtime_ns
            state.consecutive = 1
            state.stable_since = time.monotonic()
            state.locked = False
            state.read_error = ""

        # Only test readability once the size has stopped moving, to avoid
        # fighting a copy that is still in progress.
        if state.consecutive >= 1 and state.stable_since is not None:
            if time.monotonic() - state.stable_since >= self.stable_seconds:
                state.locked = is_file_locked(p)
                if not state.locked:
                    ok, err = readable(p)
                    state.read_error = "" if ok else err
                else:
                    state.read_error = "the file is locked by another program"
            else:
                state.locked = False
        return state

    def is_stable(self, state: FileStability) -> bool:
        if not state.exists or not state.size:
            return False
        if state.locked or state.read_error:
            return False
        if state.consecutive < self.stable_checks:
            return False
        if state.stable_since is None:
            return False
        return (time.monotonic() - state.stable_since) >= self.stable_seconds

    def expired(self, state: FileStability) -> bool:
        return (time.monotonic() - state.first_seen) > self.timeout

    def reason_not_stable(self, state: FileStability) -> str:
        if not state.exists:
            return "the file disappeared"
        if state.locked:
            return "the file is locked by another program"
        if state.read_error:
            return state.read_error
        if state.consecutive < self.stable_checks:
            return f"only {state.consecutive}/{self.stable_checks} size checks agreed"
        if self.is_stable(state):
            return ""
        if self.stable_seconds < 10:
            window = f"{self.stable_seconds:g}s"
        else:
            window = f"{self.stable_seconds:.0f}s"
        return f"the size has been changing for less than {window}"

    def clear_stable(self, path: Path | str) -> None:
        """Reset a file's stability (used when its content changes again)."""
        state = self._files.get(Path(path))
        if state:
            state.stable_since = time.monotonic()
            state.consecutive = 0


def wait_until_stable(
    path: Path | str,
    *,
    stable_seconds: float = 5.0,
    stable_checks: int = 2,
    poll_interval: float = 1.0,
    timeout: float = 7200.0,
    on_wait: Callable[[FileStability], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> FileStability:
    """Block until *path* is stable.  Raises :class:`StabilityTimeout`."""
    tracker = StabilityTracker(
        stable_seconds=stable_seconds,
        stable_checks=stable_checks,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_continue is not None and not should_continue():
            raise StabilityTimeout(Path(path), 0.0, tracker.poll(path))
        state = tracker.poll(path)
        if on_wait:
            on_wait(state)
        if tracker.is_stable(state):
            return state
        time.sleep(poll_interval)
    raise StabilityTimeout(Path(path), timeout, tracker.poll(path))
