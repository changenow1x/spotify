"""A dependency-free media prober.

``ffprobe`` is the gold standard, but many Windows machines do not have it and
asking a non-technical user to install it would break the "drop files in" goal.
So this module reads the container headers directly:

* **MP4 / MOV / M4A / M4V** - full ISO-BMFF atom walk (``ftyp``, ``moov``,
  ``mvhd``, ``trak``/``tkhd``/``mdia``/``hdlr``/``mdhd``/``stsd``/``stts``)
* **WAV** - ``RIFF/WAVE`` ``fmt`` + ``data`` chunks
* **MP3** - ID3 skip, first-frame header, Xing/Info/VBRI frame count
* **MPG / MPEG-PS** - pack-header scan (detection + a rough stream count); the
  exact stream parameters still need ffprobe, which the inspector says so.

Everything here is read-only, bounded in memory (box walking never loads the
whole file) and never executes anything from the file.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO, Optional

from ..core.models import MediaInfo, MediaKind, TrackInfo

__all__ = ["probe_builtin", "can_probe"]

_MAX_READ = 8 * 1024 * 1024


def can_probe(path: Path | str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in {".mp4", ".mov", ".m4v", ".m4a", ".mp3", ".wav"}


def probe_builtin(path: Path | str) -> MediaInfo:
    """Best-effort inspection with no external tools."""
    p = Path(path)
    info = MediaInfo(path=str(p), probe_source="builtin")
    try:
        info.size = p.stat().st_size
    except OSError as exc:
        info.error = f"Cannot read the file: {exc}"
        return info
    if info.size == 0:
        info.error = "The file is empty (0 bytes)."
        return info

    suffix = p.suffix.lower()
    with open(p, "rb") as fh:
        header = fh.read(min(_MAX_READ, max(4096, info.size)))
        magic = header[:16]
        try:
            if suffix in {".mp4", ".mov", ".m4v", ".m4a"} or b"ftyp" in header[:64]:
                _probe_isobmff(fh, info, header)
            elif suffix == ".wav" or header[:4] == b"RIFF":
                _probe_wav(fh, info, header)
            elif suffix == ".mp3" or header[:3] == b"ID3":
                _probe_mp3(fh, info, header)
            elif suffix in {".mpg", ".mpeg", ".m2v"} or magic[:1] == b"\x00":
                _probe_mpeg_ps(fh, info, header)
            else:
                info.error = (
                    f"Cannot inspect a .{suffix.lstrip('.')} file without ffprobe."
                )
        except Exception as exc:  # noqa: BLE001 - a bad file must never crash the worker
            info.error = f"The file could not be read as media: {type(exc).__name__}: {exc}"

    _finalize(info)
    return info


def _finalize(info: MediaInfo) -> None:
    """Derive the summary fields from the per-track data."""
    info.has_video = info.video_track_count() > 0
    info.has_audio = info.audio_track_count() > 0
    video = next((t for t in info.tracks if t.kind == "video"), None)
    audio = next((t for t in info.tracks if t.kind == "audio"), None)
    if video is not None:
        info.video_codec = info.video_codec or video.codec
        info.video_profile = info.video_profile or video.profile
        info.width = info.width or video.width
        info.height = info.height or video.height
        info.frame_rate = info.frame_rate or video.frame_rate
    if audio is not None:
        info.audio_codec = info.audio_codec or audio.codec
        info.audio_channels = info.audio_channels or audio.channels
        info.sample_rate = info.sample_rate or audio.sample_rate
        info.audio_bitrate = info.audio_bitrate or audio.bitrate
    if info.has_video:
        info.kind = MediaKind.VIDEO
    elif info.has_audio:
        info.kind = MediaKind.AUDIO
    if info.size and info.duration > 0:
        info.bitrate = int(info.size * 8 / info.duration)


# -- ISO base media file format (MP4 / MOV / M4A) ---------------------------

_VIDEO_CODEC_NAMES = {
    "avc1": "h264", "avc3": "h264", "hvc1": "hevc", "hev1": "hevc",
    "mp4v": "mpeg4", "av01": "av1", "vp08": "vp8", "vp09": "vp9",
}
_AUDIO_CODEC_NAMES = {
    "mp4a": "aac", "ac-3": "ac3", "ec-3": "eac3", "alac": "alac",
    "samr": "amr", "sawb": "amr", "Opus": "opus", "fLaC": "flac",
}

#: ISO-BMFF major brands mapped to the container name Spotify cares about.
#: Anything unlisted is an MP4-family file, which is the safe default.
_BRAND_CONTAINERS = {
    "qt  ": "mov", "QT  ": "mov",
    "m4a ": "m4a", "M4A ": "m4a",
    "m4v ": "mp4", "M4V ": "mp4",
    "3gp4": "3gp", "3g2a": "3g2",
}


def _container_from_brand(brands) -> str:
    """Map the declared ``ftyp`` brands to a container name.

    Reporting a brand verbatim (isom, mp42, avc1, ...) would make the Spotify
    spec check reject perfectly ordinary files, so only the handful of brands
    that genuinely denote a different container are mapped; everything else is
    reported as ``mp4``, which is what it is.
    """
    for brand in sorted(brands or ()):
        mapped = _BRAND_CONTAINERS.get(brand)
        if mapped:
            return mapped
    return "mp4"


def _probe_isobmff(fh: BinaryIO, info: MediaInfo, header: bytes) -> None:
    """Collect boxes into temporary state, then assemble MediaInfo."""
    state: dict = {"tracks": [], "timescale": 0, "duration": 0,
                   "brands": set(), "has_moov": False, "has_mdat": False,
                   "mdat_size": 0, "fragmented": False}
    end = info.size
    pos = 0
    while pos < end - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            break
        size, btype = struct.unpack(">I4s", head)
        btype = btype.decode("latin-1")
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size:
            break
        body_start = pos + header_size
        body_end = min(pos + size, end)

        if btype == "ftyp":
            # The ftyp box declares: major_brand (4 bytes), minor_version
            # (4 bytes) and a list of compatible brands.  Reading a fixed
            # window instead would run past the box on small files and pick up
            # unrelated four-character codes (e.g. "avc1"), which then get
            # reported as the container and make a valid MP4 look unsupported.
            fh.seek(body_start)
            data = fh.read(min(64, max(0, body_end - body_start)))
            if len(data) >= 8:
                brands = [data[0:4].decode("latin-1", "replace")]
                brands += [
                    data[i:i + 4].decode("latin-1", "replace")
                    for i in range(8, len(data) - 3, 4)
                ]
                state["brands"] = {b.strip() for b in brands if b.strip()}
        elif btype == "moov":
            state["has_moov"] = True
            _walk_moov(fh, body_start, body_end, state, depth=0)
        elif btype == "moof":
            state["fragmented"] = True
        elif btype == "mdat":
            state["has_mdat"] = True
            state["mdat_size"] += max(0, body_end - body_start)
        pos = body_end

    info.format_name = "mov,mp4,m4a" if state["brands"] else "mp4"
    info.container = _container_from_brand(state["brands"])
    if state["timescale"]:
        info.duration = state["duration"] / state["timescale"]
    info.tracks = state["tracks"]

    if state["fragmented"]:
        info.warnings.append(
            "This is a fragmented MP4. It should still work, but a "
            "progressively-downloadable file is more reliable for a large upload."
        )
    if not state["has_moov"]:
        info.error = info.error or (
            "The MP4 has no 'moov' atom, which means the file is not completely "
            "written or is corrupt."
        )
    # Codec string like "mov,mp4,m4a,3gp,3g2,mj2" -> container for reporting
    if not state["has_mdat"] and state["has_moov"]:
        info.warnings.append("No media data (mdat) was found in the file.")


def _walk_moov(fh: BinaryIO, start: int, end: int, state: dict, depth: int) -> None:
    if depth > 5:
        return
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        size, btype = struct.unpack(">I4s", head)
        btype = btype.decode("latin-1")
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size:
            return
        body_start = pos + header_size
        body_end = min(pos + size, end)

        if btype == "mvhd":
            fh.seek(body_start)
            data = fh.read(min(120, body_end - body_start))
            if len(data) >= 20:
                version = data[0]
                if version == 1 and len(data) >= 32:
                    timescale, duration = struct.unpack(">IQ", data[20:32])
                else:
                    timescale, duration = struct.unpack(">II", data[12:20])
                state["timescale"] = timescale or 1
                state["duration"] = duration
        elif btype == "trak":
            track: dict = {}
            _walk_trak(fh, body_start, body_end, track, depth + 1)
            if track.get("kind"):
                state["tracks"].append(_build_track(track))
        pos = body_end


def _walk_trak(fh: BinaryIO, start: int, end: int, track: dict, depth: int) -> None:
    if depth > 5:
        return
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        size, btype = struct.unpack(">I4s", head)
        btype = btype.decode("latin-1")
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size:
            return
        body_start = pos + header_size
        body_end = min(pos + size, end)

        if btype == "tkhd":
            fh.seek(body_start)
            data = fh.read(min(92, body_end - body_start))
            if len(data) >= 84:
                version = data[0]
                off = 4 if version == 1 else 4
                track["track_id"] = struct.unpack(">I", data[off:off + 4])[0] if len(data) >= off + 4 else 0
                if version == 1 and len(data) >= 88:
                    w, h = struct.unpack(">II", data[76:84])
                elif len(data) >= 80:
                    w, h = struct.unpack(">II", data[72:80])
                else:
                    w = h = 0
                track["width"] = w >> 16
                track["height"] = h >> 16
        elif btype == "mdia":
            _walk_mdia(fh, body_start, body_end, track, depth + 1)
        pos = body_end


def _walk_mdia(fh: BinaryIO, start: int, end: int, track: dict, depth: int) -> None:
    if depth > 5:
        return
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        size, btype = struct.unpack(">I4s", head)
        btype = btype.decode("latin-1")
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size:
            return
        body_start = pos + header_size
        body_end = min(pos + size, end)

        if btype == "mdhd":
            fh.seek(body_start)
            data = fh.read(min(44, body_end - body_start))
            if len(data) >= 24:
                version = data[0]
                if version == 1 and len(data) >= 36:
                    timescale, duration = struct.unpack(">IQ", data[20:32])
                else:
                    timescale, duration = struct.unpack(">II", data[12:20])
                track["timescale"] = timescale
                track["duration"] = duration
        elif btype == "hdlr":
            fh.seek(body_start)
            data = fh.read(min(32, body_end - body_start))
            if len(data) >= 12:
                track["kind"] = data[8:12].decode("latin-1", "replace")
                if len(data) >= 32:
                    track["language"] = data[27:29].decode("latin-1", "replace")
        elif btype == "minf":
            _walk_minf(fh, body_start, body_end, track, depth + 1)
        pos = body_end


def _walk_minf(fh: BinaryIO, start: int, end: int, track: dict, depth: int) -> None:
    if depth > 5:
        return
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        size, btype = struct.unpack(">I4s", head)
        btype = btype.decode("latin-1")
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size:
            return
        body_start = pos + header_size
        body_end = min(pos + size, end)

        if btype == "stbl":
            _walk_stbl(fh, body_start, body_end, track, depth + 1)
        pos = body_end


def _walk_stbl(fh: BinaryIO, start: int, end: int, track: dict, depth: int) -> None:
    if depth > 5:
        return
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        size, btype = struct.unpack(">I4s", head)
        btype = btype.decode("latin-1")
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size:
            return
        body_start = pos + header_size
        body_end = min(pos + size, end)

        if btype == "stsd":
            fh.seek(body_start)
            data = fh.read(min(1024, body_end - body_start))
            # Layout: [4 version/flags][4 entry_count][8 sample-entry box header]
            # followed by the Visual/Audio SampleEntry body, whose width/height
            # or channel count sit at fixed offsets from the box start.
            if len(data) >= 16:
                entry = data[12:16].decode("latin-1", "replace")
                track["codec_raw"] = entry
                # Offsets below are relative to `data`.
                if entry in _VIDEO_CODEC_NAMES:
                    track["codec"] = _VIDEO_CODEC_NAMES[entry]
                    if len(data) >= 44:
                        track["width"] = struct.unpack(">H", data[40:42])[0]
                        track["height"] = struct.unpack(">H", data[42:44])[0]
                    if entry in {"avc1", "avc3"}:
                        profile = _find_avcc_profile(data)
                        if profile:
                            track["profile"] = profile
                elif entry in _AUDIO_CODEC_NAMES:
                    track["codec"] = _AUDIO_CODEC_NAMES[entry]
                    if len(data) >= 36:
                        track["channels"] = struct.unpack(">H", data[32:34])[0]
                        bits = struct.unpack(">H", data[34:36])[0]
                        if bits:
                            track["bits"] = bits
                        if len(data) >= 44:
                            track["samplerate"] = struct.unpack(">I", data[40:44])[0] >> 16
        elif btype == "stts":
            fh.seek(body_start)
            data = fh.read(min(4096, body_end - body_start))
            if len(data) >= 8:
                count = struct.unpack(">I", data[4:8])[0]
                total_samples, total_delta = 0, 0
                offset = 8
                for _ in range(min(count, 512)):
                    if offset + 8 > len(data):
                        break
                    n, delta = struct.unpack(">II", data[offset:offset + 8])
                    total_samples += n
                    total_delta += n * delta
                    offset += 8
                if total_samples and total_delta:
                    track["avg_delta"] = total_delta / total_samples
        pos = body_end


def _find_avcc_profile(data: bytes) -> str:
    idx = data.find(b"avcC")
    if idx < 0 or idx + 6 > len(data):
        return ""
    profile_id = data[idx + 5] if idx + 5 < len(data) else 0
    return {
        0x42: "baseline", 0x4D: "main", 0x58: "extended", 0x64: "high",
    }.get(profile_id, f"profile{profile_id}")


def _build_track(track: dict) -> TrackInfo:
    kind = {"vide": "video", "soun": "audio", "subt": "subtitle",
            "text": "subtitle", "sbtl": "subtitle", "meta": "data"}.get(
        track.get("kind", ""), track.get("kind", "") or "unknown")
    timescale = track.get("timescale") or 0
    duration = track.get("duration") or 0
    seconds = duration / timescale if timescale and duration else 0.0
    if not seconds and track.get("avg_delta"):
        # fall back later; sample counts live in stsz which we do not parse
        seconds = 0.0
    frame_rate = 0.0
    if kind == "video" and track.get("avg_delta") and timescale:
        frame_rate = round(timescale / track["avg_delta"], 3) if track["avg_delta"] else 0.0
    return TrackInfo(
        index=track.get("track_id", 0),
        kind=kind,
        codec=track.get("codec", "") or track.get("codec_raw", ""),
        profile=track.get("profile", ""),
        language=track.get("language", ""),
        width=track.get("width", 0),
        height=track.get("height", 0),
        frame_rate=frame_rate,
        channels=track.get("channels", 0),
        # mdhd timescale doubles as the sample rate for audio tracks, which is
        # what every real encoder writes; esds samplerate is the fallback.
        sample_rate=timescale if kind == "audio" else 0,
        duration=seconds,
    )


# -- WAV --------------------------------------------------------------------

def _probe_wav(fh: BinaryIO, info: MediaInfo, header: bytes) -> None:
    fh.seek(0)
    riff = fh.read(12)
    if len(riff) < 12 or riff[:4] != b"RIFF":
        info.error = "Not a valid RIFF/WAVE file."
        return
    info.container = "wav"
    info.format_name = "wav"
    byte_rate = 0
    fmt: dict = {}
    pos = 12
    while pos < info.size - 8:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            break
        # RIFF chunks are a 4-character id followed by a 4-byte size.
        ctype, size = struct.unpack("<4sI", head)
        body = pos + 8
        if ctype == b"fmt ":
            data = fh.read(min(64, size))
            # WAVEFORMATEX: wFormatTag(2) nChannels(2) nSamplesPerSec(4)
            #               nAvgBytesPerSec(4) nBlockAlign(2) wBitsPerSample(2)
            if len(data) >= 16:
                (audio_format, channels, sample_rate,
                 fmt_byte_rate, block_align, bits) = struct.unpack("<HHIIHH", data[:16])
                fmt = {
                    "audio_format": audio_format,
                    "channels": channels, "sample_rate": sample_rate,
                    "bits": bits, "block_align": block_align,
                }
                if audio_format == 0xFFFE and len(data) >= 26:
                    sub = data[24:28]
                    if sub == b"flac":
                        fmt["codec"] = "flac"
                    elif sub == b"Opus":
                        fmt["codec"] = "opus"
                    elif sub[:3] == bytes(3):
                        fmt["codec"] = "pcm"
            byte_rate = fmt_byte_rate
        elif ctype == b"data":
            if fmt.get("sample_rate") and fmt.get("block_align"):
                info.duration = size / (fmt["sample_rate"] * fmt["block_align"])
            info.bitrate = byte_rate * 8
            break
        pos = body + size + (size & 1)
    codec_map = {1: "pcm_s16le", 3: "pcm_f32le", 6: "pcm", 7: "pcm", 0xFFFE: "pcm"}
    info.audio_codec = fmt.get("codec") or codec_map.get(fmt.get("audio_format", 1), "pcm")
    if fmt.get("bits") == 8:
        info.audio_codec = "pcm_u8"
    info.audio_channels = fmt.get("channels", 0)
    info.sample_rate = fmt.get("sample_rate", 0)
    info.audio_bitrate = byte_rate * 8
    if fmt:
        info.tracks = [TrackInfo(
            index=1, kind="audio", codec=info.audio_codec,
            channels=info.audio_channels, sample_rate=info.sample_rate,
            bitrate=info.audio_bitrate, duration=info.duration,
        )]


# -- MP3 --------------------------------------------------------------------

# MPEG-1/2/2.5 audio frame header, ISO/IEC 11172-3 & 13818-3.
#
#   byte 0: 11111111                      (frame sync)
#   byte 1: 11 | ver(2) | layer(2) | prot(1)
#   byte 2: bitrate(4) | samplerate(2) | pad(1) | priv(1)
#   byte 3: mode(2) | mode_ext(2) | copy(1) | orig(1) | emphasis(2)
#
# ``version_id``: 0 = MPEG 2.5, 1 = reserved, 2 = MPEG 2, 3 = MPEG 1.
# ``layer`` is 4 - layer_bits, so 1 = Layer I, 2 = Layer II, 3 = Layer III.
_VERSION = {3: "MPEG1", 2: "MPEG2", 0: "MPEG2.5"}
_MPEG_BITRATES = {
    (3, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
    (3, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (0, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    (0, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (0, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}
_MPEG_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
#: Samples per frame, keyed by (version, layer).
_SAMPLES_PER_FRAME = {
    (3, 1): 384, (3, 2): 1152, (3, 3): 1152,
    (2, 1): 384, (2, 2): 1152, (2, 3): 576,
    (0, 1): 384, (0, 2): 1152, (0, 3): 576,
}
#: Side-information size in bytes, keyed by (version, channels).
_SIDE_INFO = {
    (3, 2): 32, (3, 1): 17,
    (2, 2): 17, (2, 1): 9,
    (0, 2): 17, (0, 1): 9,
}


def _probe_mp3(fh: BinaryIO, info: MediaInfo, header: bytes) -> None:
    info.container = "mp3"
    info.format_name = "mp3"
    pos = 0
    if header[:3] == b"ID3":
        size_bytes = header[6:10]
        if len(size_bytes) == 4:
            syncsafe = 0
            for byte in size_bytes:
                syncsafe = (syncsafe << 7) | (byte & 0x7F)
            pos = 10 + syncsafe
    fh.seek(pos)
    buf = fh.read(min(_MAX_READ, max(65536, info.size - pos)))
    frame = _find_mp3_frame(buf)
    if frame is None:
        info.error = "No valid MP3 frame was found; the file may be corrupt."
        return
    bitrate_kbps, sample_rate, channels, length = frame
    info.audio_codec = "mp3"
    info.audio_channels = channels
    info.sample_rate = sample_rate
    info.audio_bitrate = bitrate_kbps * 1000
    info.bitrate = info.audio_bitrate
    if length:
        info.duration = length
    else:
        info.duration = (info.size - pos) * 8 / max(1, info.audio_bitrate)
    info.tracks = [TrackInfo(
        index=1, kind="audio", codec="mp3", channels=channels,
        sample_rate=sample_rate, bitrate=info.audio_bitrate, duration=info.duration,
    )]


def _find_mp3_frame(buf: bytes) -> Optional[tuple[int, int, int, float]]:
    """Return ``(bitrate_kbps, sample_rate, channels, xing_duration_seconds)``."""
    limit = min(len(buf) - 4, 256 * 1024)
    i = 0
    while i < limit:
        if buf[i] != 0xFF or (buf[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        b1, b2, b3 = buf[i + 1], buf[i + 2], buf[i + 3]
        version_id = (b1 >> 3) & 0x03
        layer_bits = (b1 >> 1) & 0x03
        if version_id == 1 or layer_bits == 0:
            i += 1
            continue
        layer = 4 - layer_bits
        bitrate_index = (b2 >> 4) & 0x0F
        rate_index = (b2 >> 2) & 0x03
        if bitrate_index in (0, 15) or rate_index == 3:
            i += 1
            continue
        table = _MPEG_BITRATES.get((version_id, layer))
        rates = _MPEG_RATES.get(version_id)
        if not table or not rates:
            i += 1
            continue
        bitrate_kbps = table[bitrate_index]
        sample_rate = rates[rate_index]
        if not bitrate_kbps or not sample_rate:
            i += 1
            continue
        mode = (b3 >> 6) & 0x03
        channels = 1 if mode == 3 else 2
        spf = _SAMPLES_PER_FRAME.get((version_id, layer), 1152)
        side_info = _SIDE_INFO.get((version_id, channels), 17)

        # A Xing/Info (or VBRI) header right after the side info gives the
        # exact frame count, hence an exact duration even for VBR files.
        duration = 0.0
        side_at = i + 4 + side_info
        window = buf[side_at - 12: side_at + 16] if side_at + 16 <= len(buf) else b""
        idx_x, idx_i = window.find(b"Xing"), window.find(b"Info")
        if idx_x >= 0 or idx_i >= 0:
            tag_at = side_at - 12 + max(idx_x, idx_i)
            if tag_at + 12 <= len(buf):
                flags = struct.unpack(">I", buf[tag_at + 4: tag_at + 8])[0]
                if flags & 0x01:
                    frames = struct.unpack(">I", buf[tag_at + 8: tag_at + 12])[0]
                    duration = frames * spf / sample_rate
        if not duration and b"VBRI" in buf[i:i + 64]:
            vb = buf.find(b"VBRI", i, i + 64)
            if vb > 0 and vb + 18 <= len(buf):
                frames = struct.unpack(">I", buf[vb + 14:vb + 18])[0]
                duration = frames * spf / sample_rate
        return bitrate_kbps, sample_rate, channels, duration
    return None


# -- MPEG program stream (.mpg) --------------------------------------------

def _probe_mpeg_ps(fh: BinaryIO, info: MediaInfo, header: bytes) -> None:
    """Detect the container and count elementary streams (no codec details)."""
    info.container = "mpg"
    info.format_name = "mpeg"
    buf = header[:65536]
    video_streams = buf.count(b"\x00\x00\x01\xe0")
    audio_streams = buf.count(b"\x00\x00\x01\xc0") + buf.count(b"\x00\x00\x01\xdf")
    pack_count = buf.count(b"\x00\x00\x01\xba")
    if pack_count == 0 and not (video_streams or audio_streams):
        info.error = "No MPEG program stream was found; the file may be corrupt."
        return
    if video_streams:
        info.tracks.append(TrackInfo(index=0, kind="video", codec="mpeg2"))
    if audio_streams:
        info.tracks.append(TrackInfo(index=len(info.tracks), kind="audio", codec="mp2"))
    info.warnings.append(
        "MPEG program streams (.mpg) are supported by Spotify, but this "
        "application cannot read their detailed codec settings without ffprobe. "
        "Install ffmpeg for a full pre-flight check, or upload an MP4 instead."
    )
    info.error = ""
