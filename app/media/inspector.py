"""Media inspection.

``inspect()`` always tries ``ffprobe`` first (most accurate) and transparently
falls back to the built-in header parser when ffprobe is not installed.  The
result records *which* tool produced it, so the dashboard can tell the user
"checked with ffprobe" versus "checked with the built-in reader" instead of
implying a level of verification that did not happen.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.models import MediaInfo, MediaKind, TrackInfo
from ..security.redaction import redact_text
from .builtin_probe import can_probe, probe_builtin
from .ffmpeg import FfmpegTools, ToolNotFound

__all__ = ["MediaInspector", "InspectionResult", "classify_extension"]


def classify_extension(path: Path | str, policy=None) -> MediaKind:
    ext = Path(path).suffix.lower()
    audio = {".mp3", ".m4a", ".wav"}
    video = {".mp4", ".mov", ".mpg", ".mpeg", ".m4v"}
    if policy is not None:
        audio = {e.lower() for e in policy.audio_extensions}
        video = {e.lower() for e in policy.video_extensions}
    if ext in audio:
        return MediaKind.AUDIO
    if ext in video:
        return MediaKind.VIDEO
    return MediaKind.UNKNOWN


@dataclass
class InspectionResult:
    info: MediaInfo
    elapsed_ms: int = 0
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    @property
    def reliable(self) -> bool:
        return self.info.probe_source == "ffprobe" and not self.info.error


class MediaInspector:
    def __init__(self, tools: FfmpegTools, policy=None):
        self.tools = tools
        self.policy = policy

    def supported(self, path: Path | str) -> bool:
        ext = Path(path).suffix.lower()
        if self.policy is not None:
            return ext in set(self.policy.all_media_extensions())
        return ext in {".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mpg", ".mpeg", ".m4v"}

    def inspect(self, path: Path | str, *, timeout: float = 180.0) -> InspectionResult:
        start = time.perf_counter()
        p = Path(path)
        try:
            st = p.stat()
        except OSError as exc:
            info = MediaInfo(path=str(p), error=f"The file cannot be read: {exc}")
            return InspectionResult(info, int((time.perf_counter() - start) * 1000))

        if st.st_size == 0:
            info = MediaInfo(path=str(p), size=0, error="The file is empty (0 bytes).")
            return InspectionResult(info, int((time.perf_counter() - start) * 1000))

        expected = classify_extension(p, self.policy)

        if self.tools.available():
            try:
                result = self.tools.probe(p, timeout=timeout)
                if result.ok:
                    info = self._from_ffprobe(p, result.data, st.st_size, expected)
                    info.warnings.extend(result.warnings if hasattr(result, "warnings") else [])
                    return InspectionResult(info, result.duration_ms or int((time.perf_counter() - start) * 1000))
                ff_error = result.error
            except ToolNotFound:
                ff_error = ""
            except (TimeoutError, OSError) as exc:
                ff_error = redact_text(str(exc))
        else:
            ff_error = ""

        # Fallback: the built-in header reader.
        if can_probe(p):
            info = probe_builtin(p)
            info.kind = expected if info.kind is MediaKind.UNKNOWN else info.kind
            if ff_error:
                info.warnings.append(
                    f"ffprobe was available but reported a problem ({ff_error}); "
                    f"the built-in reader was used instead."
                )
            elapsed = int((time.perf_counter() - start) * 1000)
            return InspectionResult(info, elapsed)

        info = MediaInfo(
            path=str(p), size=st.st_size, kind=expected, probe_source="unavailable",
            error=ff_error or (
                f"No media tool could read this .{p.suffix.lstrip('.')} file. "
                f"Install ffmpeg (see SETUP.bat) for full inspection."
            ),
        )
        return InspectionResult(info, int((time.perf_counter() - start) * 1000))

    # -- ffprobe JSON -> MediaInfo ---------------------------------------
    def _from_ffprobe(self, path: Path, data: dict, size: int, expected: MediaKind) -> MediaInfo:
        fmt = data.get("format", {}) or {}
        streams = data.get("streams", []) or []
        info = MediaInfo(
            path=str(path),
            probe_source="ffprobe",
            container=(fmt.get("format_name") or "").split(",")[0] if fmt.get("format_name") else "",
            format_name=fmt.get("format_name", ""),
            size=int(fmt.get("size") or size),
        )
        try:
            info.duration = float(fmt.get("duration") or 0.0)
        except (TypeError, ValueError):
            info.duration = 0.0
        try:
            info.bitrate = int(float(fmt.get("bit_rate") or 0))
        except (TypeError, ValueError):
            info.bitrate = 0

        for index, stream in enumerate(streams):
            kind = (stream.get("codec_type") or "").lower()
            if kind not in {"video", "audio"}:
                continue
            track = TrackInfo(
                index=int(stream.get("index") or index),
                kind=kind,
                codec=(stream.get("codec_name") or "").lower(),
                profile=(stream.get("profile") or "").lower(),
                language=((stream.get("tags") or {}).get("language") or "")[:5],
            )
            try:
                track.duration = float(stream.get("duration") or 0.0)
            except (TypeError, ValueError):
                track.duration = 0.0
            try:
                track.bitrate = int(float(stream.get("bit_rate") or 0))
            except (TypeError, ValueError):
                track.bitrate = 0
            if kind == "video":
                track.width = int(stream.get("width") or 0)
                track.height = int(stream.get("height") or 0)
                rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0"
                try:
                    num, _, den = rate.partition("/")
                    if float(den or 0) > 0:
                        track.frame_rate = round(float(num) / float(den), 3)
                except (TypeError, ValueError, ZeroDivisionError):
                    track.frame_rate = 0.0
                info.width = track.width
                info.height = track.height
                info.frame_rate = track.frame_rate
                info.video_codec = track.codec
                info.video_profile = track.profile
                info.pix_fmt = (stream.get("pix_fmt") or "")
                color = stream.get("color_space") or ""
                info.color_space = color
                field_order = (stream.get("field_order") or "")
                info.field_order = "" if field_order == "unknown" else field_order
            else:
                track.channels = int(stream.get("channels") or 0)
                track.sample_rate = int(stream.get("sample_rate") or 0)
                info.audio_codec = track.codec
                info.audio_channels = track.channels
                info.sample_rate = track.sample_rate
                info.audio_bitrate = track.bitrate or (info.bitrate if len(streams) == 1 else 0)
            info.tracks.append(track)

        info.has_video = info.video_track_count() > 0
        info.has_audio = info.audio_track_count() > 0
        info.kind = MediaKind.VIDEO if info.has_video else (MediaKind.AUDIO if info.has_audio else expected)
        if not info.tracks:
            info.error = "No audio or video stream was found in this file."
        if info.duration and any(t.duration for t in info.tracks):
            durations = [t.duration for t in info.tracks if t.duration]
            if durations and max(durations) - min(durations) > 1.0:
                info.warnings.append(
                    f"The audio and video tracks have noticeably different lengths "
                    f"({min(durations):.1f}s vs {max(durations):.1f}s). Spotify asks for "
                    f"audio and video of the same duration."
                )
        return info

    def cache_json(self, info: MediaInfo) -> str:
        return json.dumps(info.as_dict(), default=str)
