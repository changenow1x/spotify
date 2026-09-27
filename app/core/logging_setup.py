"""Structured local logging.

Three outputs, all local, none of which ever receives a secret:

``app.log``     rotating technical log, ``[YYYY-MM-DD HH:MM:SS] LEVEL message``
``activity.log`` one line per *user-visible* event, the exact format the
                specification asks for, and what the dashboard timeline reads
``diagnostics/`` HTML + PNG snapshots captured when Spotify's UI misbehaves

A :class:`RedactingFilter` is installed on every handler, so even a stray
exception message cannot leak a token into a file.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from ..security.redaction import redact_text

__all__ = ["setup_logging", "get_logger", "log_activity", "activity_tail", "LoggingBundle"]

_LINE_FORMAT = "[%(asctime)s] %(levelname)-8s %(name)-28s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
#: The exact shape the specification shows in its example log.
_ACTIVITY_FORMAT = "[%(asctime)s] %(message)s"


class RedactingFilter(logging.Filter):
    """Masks secrets on their way to *any* handler.

    Order matters.  Interpolating first and redacting the result would let a
    secret back in through ``record.args``; redacting the *format string*
    first would eat the ``%s`` placeholders and break ``msg % args`` entirely
    (which leaks the raw values in ``logging``'s own error report).  So the
    values are redacted, the record is collapsed into a finished message, and
    only then is the text masked.
    """

    @staticmethod
    def _mask(value: object) -> object:
        # Only text can carry a secret, and stringifying a number would break
        # the ``%d``/``%f`` placeholders in the record's format string.
        return redact_text(value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: self._mask(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(self._mask(v) for v in record.args)
                # Collapse now; leaving a redacted format string behind would
                # raise on the next handler and echo the unredacted arguments.
                record.msg = record.getMessage()
                record.args = ()
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg, max_len=8000)
            if record.exc_text:
                # Never let an exception object print a secret either.
                record.exc_text = redact_text(record.exc_text, max_len=4000)
        except Exception:  # noqa: BLE001 - a broken filter must not drop the record
            pass
        return True


class _ActivityHandler(logging.Handler):
    """Writes the user-facing activity log."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            path = getattr(self, "path", None)
            if not path:
                return
            line = f"[{time.strftime(_DATE_FORMAT, time.localtime(record.created))}] {record.getMessage()}\n"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(redact_text(line, max_len=4000).replace("\n", " ") if "\r" not in line else line)
        except Exception:  # noqa: BLE001  # pragma: no cover
            self.handleError(record)


class LoggingBundle:
    def __init__(self) -> None:
        self.app_log: Optional[Path] = None
        self.activity_log: Optional[Path] = None
        self.diagnostics_dir: Optional[Path] = None
        self._activity_lock = threading.Lock()


_BUNDLE = LoggingBundle()


def setup_logging(
    *,
    log_dir: Path,
    level: str = "INFO",
    console_level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
    activity_file: str = "activity.log",
    retain_days: int = 90,
    quiet: bool = False,
) -> LoggingBundle:
    """Configure the root logger once.  Safe to call repeatedly."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = log_dir / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001  # pragma: no cover
            pass

    redact_filter = RedactingFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    file_handler.setFormatter(logging.Formatter(_LINE_FORMAT, _DATE_FORMAT))
    file_handler.addFilter(redact_filter)
    root.addHandler(file_handler)

    activity_handler = _ActivityHandler()
    activity_handler.setLevel(logging.INFO)
    activity_handler.setFormatter(logging.Formatter(_ACTIVITY_FORMAT, _DATE_FORMAT))
    activity_handler.addFilter(redact_filter)
    activity_handler.path = log_dir / activity_file
    root.addHandler(activity_handler)

    if not quiet:
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
        console.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"
        ))
        console.addFilter(redact_filter)
        root.addHandler(console)

    # Third-party chatter
    for noisy in ("watchdog", "watchdog.observers", "asyncio", "httpx", "httpcore",
                  "urllib3", "multipart", "websockets", "browserforge"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _BUNDLE.app_log = log_dir / "app.log"
    _BUNDLE.activity_log = log_dir / activity_file
    _BUNDLE.diagnostics_dir = diagnostics
    _purge_old_logs(diagnostics, retain_days)
    return _BUNDLE


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_activity(message: str, *, level: int = logging.INFO) -> None:
    """Write one user-facing line to ``activity.log`` and ``app.log``."""
    logging.getLogger("activity").log(level, message)


def activity_tail(lines: int = 200) -> list[str]:
    """Read the last *lines* lines of the activity log."""
    path = _BUNDLE.activity_log
    if not path or not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:  # pragma: no cover
        return []
    return content.splitlines()[-lines:]


def _purge_old_logs(directory: Path, retain_days: int) -> None:
    if retain_days <= 0 or not directory.exists():
        return
    cutoff = time.time() - retain_days * 86400
    try:
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:  # pragma: no cover
                continue
    except OSError:  # pragma: no cover
        pass
