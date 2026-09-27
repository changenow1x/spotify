"""Thin, safe wrapper around ``ffprobe`` / ``ffmpeg``.

Hard rules enforced here:
* subprocesses are always invoked with an **argument list** (``shell=False``),
  so a filename containing ``&``, ``|``, ``;`` or ``$(...)`` cannot become a
  shell command;
* every ``shell=True``-style interpolation path is forbidden by construction;
* timeouts are always set, so a corrupt file cannot hang the worker;
* output is size-capped, so a hostile file cannot exhaust memory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = [
    "ToolNotFound",
    "FfprobeResult",
    "FfmpegTools",
    "MAX_OUTPUT_BYTES",
    "safe_run",
]

MAX_OUTPUT_BYTES = 32 * 1024 * 1024
#: Windows kills processes without this flag when the parent exits.
_NO_WINDOW = getattr(os, "STARTUPINFO", None)


class ToolNotFound(RuntimeError):
    def __init__(self, tool: str, hint: str = ""):
        self.tool = tool
        super().__init__(
            f"{tool} was not found on this computer."
            + (f" {hint}" if hint else "")
        )


@dataclass
class FfprobeResult:
    ok: bool
    data: dict
    error: str = ""
    duration_ms: int = 0


def _startupinfo():  # pragma: no cover - Windows only
    if _NO_WINDOW is None:
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def safe_run(
    args: Sequence[str],
    *,
    timeout: float = 120.0,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command given as an argument list, never through a shell."""
    if not args:
        raise ValueError("safe_run requires a non-empty argument list")
    kwargs: dict = {
        "shell": False,
        "capture_output": True,
        "timeout": timeout,
    }
    startupinfo = _startupinfo()
    if startupinfo is not None:  # pragma: no cover - Windows only
        kwargs["startupinfo"] = startupinfo
    try:
        proc = subprocess.run(list(args), **kwargs)  # noqa: S603 - shell=False, fixed argv
    except FileNotFoundError as exc:
        raise ToolNotFound(args[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"{Path(args[0]).name} did not finish within {timeout:.0f}s. The file may be corrupt."
        ) from exc
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, args, output=proc.stdout, stderr=proc.stderr
        )
    return proc


class FfmpegTools:
    """Locates ffprobe/ffmpeg and exposes the one call we actually need."""

    def __init__(self, ffprobe_path: str = "", ffmpeg_path: str = ""):
        self._ffprobe = ffprobe_path or ""
        self._ffmpeg = ffmpeg_path or ""
        self._probed: bool = False
        self._version: str = ""

    # -- discovery --------------------------------------------------------
    @property
    def ffprobe(self) -> str:
        if not self._probed:
            self._locate()
        return self._ffprobe

    @property
    def ffmpeg(self) -> str:
        if not self._probed:
            self._locate()
        return self._ffmpeg

    def _locate(self) -> None:
        self._probed = True
        if self._ffprobe:
            self._ffprobe = self._absolute(self._ffprobe)
            if not self._ffprobe:
                self._ffprobe = ""
        if not self._ffprobe:
            found = shutil.which("ffprobe")
            if found:
                self._ffprobe = found
        if self._ffmpeg:
            self._ffmpeg = self._absolute(self._ffmpeg)
            if not self._ffmpeg:
                self._ffmpeg = ""
        if not self._ffmpeg:
            found = shutil.which("ffmpeg")
            if found:
                self._ffmpeg = found

    @staticmethod
    def _absolute(path: str) -> str:
        p = Path(path).expanduser()
        if p.is_file():
            return str(p)
        # Common Windows install locations, checked so the user does not have to
        # add ffmpeg to PATH.
        for candidate in (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg/bin/ffprobe.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg/ffprobe.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages/ffprobe.exe",
            Path("/usr/bin/ffprobe"),
            Path("/usr/local/bin/ffprobe"),
            Path("/opt/homebrew/bin/ffprobe"),
        ):
            if candidate.is_file():
                return str(candidate)
        return ""

    def available(self) -> bool:
        return bool(self.ffprobe)

    def version(self) -> str:
        if not self.available():
            return ""
        if not self._version:
            try:
                proc = safe_run([self.ffprobe, "-version"], timeout=20)
                first = proc.stdout.decode("utf-8", "replace").splitlines()
                self._version = first[0] if first else ""
            except (ToolNotFound, TimeoutError, OSError):  # pragma: no cover
                self._version = ""
        return self._version

    # -- probing ----------------------------------------------------------
    def probe(
        self,
        path: Path | str,
        *,
        timeout: float = 180.0,
    ) -> FfprobeResult:
        """Return full ffprobe stream/format information for *path*.

        The file path is passed as a single argv element, so it can contain
        spaces, quotes, ampersands or any other character safely.
        """
        import time

        if not self.available():
            raise ToolNotFound("ffprobe")
        target = str(Path(path).resolve(strict=False))
        args = [
            self.ffprobe,
            "-hide_banner",
            "-loglevel", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            # Reading packets is what makes ffprobe report real stream
            # timestamps/bitrates, but it is slow for 6 GB files, so we do not.
            "-i", target,
        ]
        start = time.perf_counter()
        try:
            proc = safe_run(args, timeout=timeout)
        except TimeoutError as exc:
            return FfprobeResult(False, {}, str(exc), int((time.perf_counter() - start) * 1000))
        elapsed = int((time.perf_counter() - start) * 1000)
        stdout = proc.stdout[:MAX_OUTPUT_BYTES]
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:2000]
        if proc.returncode != 0 and not stdout:
            return FfprobeResult(False, {}, stderr or f"ffprobe exited with {proc.returncode}", elapsed)
        try:
            data = json.loads(stdout.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError as exc:
            return FfprobeResult(False, {}, f"ffprobe returned unreadable JSON: {exc}", elapsed)
        if not data.get("streams") and not data.get("format"):
            return FfprobeResult(False, {}, stderr or "ffprobe found no readable streams", elapsed)
        return FfprobeResult(True, data, "", elapsed)

    def probe_integrity(self, path: Path | str, *, timeout: float = 600.0) -> FfprobeResult:
        """Deeper check: actually decodes headers and reports read errors."""
        if not self.available():
            raise ToolNotFound("ffprobe")
        args = [
            self.ffprobe, "-hide_banner", "-v", "error",
            "-show_entries", "format=duration,size,format_name",
            "-show_error", "-i", str(Path(path).resolve(strict=False)),
        ]
        try:
            proc = safe_run(args, timeout=timeout)
        except TimeoutError as exc:
            return FfprobeResult(False, {}, str(exc))
        return FfprobeResult(proc.returncode == 0, {}, proc.stderr.decode("utf-8", "replace")[:2000])

    def derive_copy(
        self,
        src: Path | str,
        dest: Path | str,
        *,
        kind: str,
        timeout: float = 14_400.0,
    ) -> tuple[bool, str]:
        """Create a *derived copy* of the media (never touch the original).

        Only used when a file is playable but outside what Spotify accepts.
        The original file is left completely untouched.
        """
        src_p, dest_p = Path(src), Path(dest)
        if not self.ffmpeg:
            return False, "ffmpeg is not installed, so the file cannot be converted."
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        common = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-nostdin", "-y", "-i", str(src_p.resolve(strict=False)),
        ]
        if kind == "audio":
            args = common + ["vn", "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
                             "-ac", "2", str(dest_p)]
        else:
            args = common + [
                "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(dest_p),
            ]
        try:
            proc = safe_run(args, timeout=timeout)
        except TimeoutError as exc:
            return False, str(exc)
        except ToolNotFound as exc:
            return False, str(exc)
        if proc.returncode != 0:
            return False, proc.stderr.decode("utf-8", "replace")[:2000]
        if not dest_p.is_file() or dest_p.stat().st_size == 0:
            return False, "ffmpeg reported success but produced no output file."
        return True, ""
