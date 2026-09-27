"""Generate real media files for tests and for ``python -m app --make-test-media``.

No ffmpeg required.  What each generator produces:

``wav``  a genuinely valid, playable PCM RIFF/WAVE file.
``mp3``  a genuinely valid MPEG-1 Layer III stream of silent frames (real
        frame headers, ID3 tag, Xing/Info frame count), so decoders and our
        own prober both read a correct duration.
``mp4``  a structurally valid ISO-BMFF file with correct ``ftyp``/``moov``/
        ``trak``/``stsd`` atoms, one ``avc1`` video track and one ``mp4a``
        audio track.  The payload is synthetic, so it is *not* playable - it is
        a container-accurate fixture for exercising inspection and validation.
        For a playable file, point the app at real media or install ffmpeg.
``mpg``  a minimal MPEG program stream header sequence.

These are honest fixtures: they let the whole pipeline be tested offline, and
the docs say plainly which of them are playable.
"""

from __future__ import annotations

import struct
from pathlib import Path

__all__ = ["make_wav", "make_mp3", "make_mp4", "make_mpg", "write_test_episode",
           "SUPPORTED_FIXTURES"]

SUPPORTED_FIXTURES = ("wav", "mp3", "mp4", "mov", "mpg")


# ---------------------------------------------------------------- WAV ------

def make_wav(path: Path | str, *, seconds: float = 3.0, sample_rate: int = 44100,
             channels: int = 2, bits: int = 16, silence: bool = True) -> Path:
    """Write a real PCM WAV file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_per_sample = bits // 8
    block_align = channels * bytes_per_sample
    frame_count = int(seconds * sample_rate)
    data = b"\x00" * (frame_count * block_align)
    byte_rate = sample_rate * block_align

    fmt_chunk = struct.pack(
        "<4sIHHIIHH", b"fmt ", 16, 1, channels, sample_rate,
        byte_rate, block_align, bits,
    )
    fact = b""
    if not silence:
        # A quiet 440 Hz tone, so the file is audibly real.
        import math
        samples = bytearray()
        for i in range(frame_count):
            value = int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            for _ in range(channels):
                samples += struct.pack("<h", value)
        data = bytes(samples)
    else:
        fact = struct.pack("<4sII", b"fact", 4, frame_count)

    body = b"WAVE" + fmt_chunk + fact + struct.pack("<4sI", b"data", len(data)) + data
    header = struct.pack("<4sI", b"RIFF", 4 + len(body))
    path.write_bytes(header + body)
    return path


# ---------------------------------------------------------------- MP3 ------

_MP3_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]


def _mp3_frame(bitrate_kbps: int = 128, sample_rate: int = 44100) -> bytes:
    """One valid MPEG-1 Layer III frame header plus a silent main-data body."""
    bitrate_index = _MP3_BITRATES.index(bitrate_kbps) if bitrate_kbps in _MP3_BITRATES else 9
    rate_index = {44100: 0, 48000: 1, 32000: 2}[sample_rate]
    # byte1: 11111111 111 11 01  (MPEG1, Layer III, no CRC)
    b1 = 0xFF
    b2 = 0xFB
    b3 = (bitrate_index << 4) | (rate_index << 2)  # stereo (00 = stereo)
    frame_len = int(144 * bitrate_kbps * 1000 / sample_rate)
    return bytes([b1, b2, b3]) + b"\x00" * (frame_len - 3)


def make_mp3(path: Path | str, *, seconds: float = 3.0, bitrate_kbps: int = 128,
             sample_rate: int = 44100, title: str = "Test Episode",
             comment: str = "") -> Path:
    """Write a real MPEG-1 Layer III file of silent frames with an ID3 tag.

    ``comment`` (or a distinct ``title``) makes two otherwise identical files
    differ byte for byte.  Tests that need several *different* episodes must
    vary it, because the app identifies an episode by content hash: two
    byte-identical files really are the same episode and are deduplicated.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = _mp3_frame(bitrate_kbps, sample_rate)
    frames = max(1, int(seconds * sample_rate / 1152))  # 1152 samples per MPEG1 Layer III frame
    # Xing/Info header in the *first* frame so the duration is exact.
    # 0x03 = frame count + byte count present.
    xing = b"Info" + struct.pack(">I", 0x03) + struct.pack(">I", frames) + \
        struct.pack(">I", frames * 1152)
    # The header has to fit inside the first frame, and the remaining frames
    # must all still be written - otherwise the file would claim a duration its
    # byte count cannot support, and every fixture would look truncated.
    if 36 + len(xing) > len(frame):
        raise ValueError("The Xing header does not fit in one frame.")
    first = frame[:36] + xing + frame[36 + len(xing):]
    audio = first + frame * (frames - 1)

    def _syncsafe(n: int) -> bytes:
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])

    text = title.encode("latin-1", "replace")
    body = b"TIT2" + struct.pack(">I", len(text)) + b"\x00\x00" + text
    if comment:
        note = comment.encode("latin-1", "replace")
        body += b"COMM" + struct.pack(">I", len(note)) + b"\x00\x00\x00" + note
    frame_body = body
    id3 = b"ID3\x03\x00\x00" + _syncsafe(len(frame_body)) + frame_body

    path.write_bytes(id3 + audio)
    return path


# ---------------------------------------------------------------- MP4 ------

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _full(kind: bytes, version: int, flags: bytes, payload: bytes) -> bytes:
    return _box(kind, bytes([version]) + flags + payload)


def make_mp4(
    path: Path | str,
    *,
    seconds: float = 3.0,
    width: int = 1920,
    height: int = 1080,
    video_codec: str = "avc1",
    audio_codec: str = "mp4a",
    fps: int = 30,
    sample_rate: int = 44100,
    channels: int = 2,
    video_tracks: int = 1,
    audio_tracks: int = 1,
    with_audio: bool = True,
    container: str = "isom",
    mdat_bytes: int = 4096,
) -> Path:
    """Write a container-accurate MP4 with the requested track layout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timescale = 1000
    duration = int(seconds * timescale)

    ftyp = _box(b"ftyp", container.encode() + struct.pack(">I", 512) +
                b"isom" + b"iso2" + b"avc1" + b"mp41")

    mvhd = _full(b"mvhd", 0, b"\x00\x00\x00", struct.pack(
        ">IIII", 0, 0, timescale, duration) + struct.pack(">iHH", 0x00010000, 0x0100, 0) +
        b"\x00" * 8 + struct.pack(
            ">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) +
        b"\x00" * 24 + struct.pack(">I", video_tracks + audio_tracks + 1))

    def video_trak(track_id: int) -> bytes:
        tkhd = _full(b"tkhd", 0, b"\x00\x00\x07", struct.pack(
            ">IIIII", 0, 0, track_id, 0, int(seconds * 1000)) +
            b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0) +
            struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) +
            struct.pack(">II", width << 16, height << 16))
        mdhd = _full(b"mdhd", 0, b"\x00\x00\x00",
                     struct.pack(">IIII", 0, 0, timescale, duration) +
                     struct.pack(">HH", 0x55C4, 0))
        hdlr = _full(b"hdlr", 0, b"\x00\x00\x00", struct.pack(">I", 0) + b"vide" +
                     b"\x00" * 12 + b"VideoHandler\x00")
        avcc = b"\x01\x64\x00\x1f\xff\xe1\x00\x04\x67\x64\x00\x1f"  # High profile
        # VisualSampleEntry, ISO/IEC 14496-12: 6 reserved + data_reference_index,
        # 2 pre_defined + 2 reserved + 12 pre_defined, then width/height.
        visual = (b"\x00" * 6 + struct.pack(">H", 1) +
                  struct.pack(">HH", 0, 0) + b"\x00" * 12 +
                  struct.pack(">HH", width, height) +
                  struct.pack(">II", 0x00480000, 0x00480000) +
                  struct.pack(">I", 0) + struct.pack(">H", 1) +
                  b"\x00" * 32 + struct.pack(">Hh", 24, -1))
        avc1 = _box(b"avc1", visual + _box(b"avcC", avcc))
        stsd = _full(b"stsd", 0, b"\x00\x00\x00", struct.pack(">I", 1) + avc1)
        stts = _full(b"stts", 0, b"\x00\x00\x00",
                     struct.pack(">III", 1, int(seconds * fps),
                                 max(1, int(timescale / fps))))
        stbl = _box(b"stbl", stsd + stts +
                    _full(b"stsc", 0, b"\x00\x00\x00", struct.pack(">II", 0, 0)) +
                    _full(b"stsz", 0, b"\x00\x00\x00", struct.pack(">II", 0, 0)) +
                    _full(b"stco", 0, b"\x00\x00\x00", struct.pack(">II", 0, 0)))
        minf = _box(b"minf", _full(b"vmhd", 0, b"\x00\x00\x01", b"\x00" * 8) + stbl)
        mdia = _box(b"mdia", mdhd + hdlr + minf)
        return _box(b"trak", tkhd + mdia)

    def audio_trak(track_id: int) -> bytes:
        tkhd = _full(b"tkhd", 0, b"\x00\x00\x07", struct.pack(
            ">IIIII", 0, 0, track_id, 0, int(seconds * 1000)) +
            b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0) +
            struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) +
            struct.pack(">II", 0, 0))
        mdhd = _full(b"mdhd", 0, b"\x00\x00\x00",
                     struct.pack(">IIII", 0, 0, sample_rate, int(seconds * sample_rate)) +
                     struct.pack(">HH", 0x55C4, 0))
        hdlr = _full(b"hdlr", 0, b"\x00\x00\x00", struct.pack(">I", 0) + b"soun" +
                     b"\x00" * 12 + b"SoundHandler\x00")
        # AudioSampleEntry: 6 reserved + data_reference_index, 8 reserved,
        # then channelcount and samplesize.
        audio_entry = (b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8 +
                       struct.pack(">HH", channels, 16) +
                       struct.pack(">HH", 0, 0) +
                       struct.pack(">I", sample_rate << 16))
        mp4a = _box(b"mp4a", audio_entry + _box(b"esds", b"\x00" * 4))
        stsd = _full(b"stsd", 0, b"\x00\x00\x00", struct.pack(">I", 1) + mp4a)
        stts = _full(b"stts", 0, b"\x00\x00\x00", struct.pack(">II", 1, 1))
        stbl = _box(b"stbl", stsd + stts +
                    _full(b"stsc", 0, b"\x00\x00\x00", struct.pack(">II", 0, 0)) +
                    _full(b"stsz", 0, b"\x00\x00\x00", struct.pack(">II", 0, 0)) +
                    _full(b"stco", 0, b"\x00\x00\x00", struct.pack(">II", 0, 0)))
        smhd = _full(b"smhd", 0, b"\x00\x00\x00", struct.pack(">hh", 0, 0))
        dinf = _box(b"dinf", _full(b"dref", 0, b"\x00\x00\x00",
                                   struct.pack(">I", 1) + _full(b"url ", 0, b"\x00\x00\x01", b"")))
        minf = _box(b"minf", smhd + dinf + stbl)
        mdia = _box(b"mdia", mdhd + hdlr + minf)
        return _box(b"trak", tkhd + mdia)

    traks = b"".join(video_trak(i + 1) for i in range(video_tracks))
    if with_audio:
        traks += b"".join(audio_trak(video_tracks + i + 1) for i in range(audio_tracks))
    moov = _box(b"moov", mvhd + traks)
    mdat = _box(b"mdat", b"\x00" * mdat_bytes)
    path.write_bytes(ftyp + moov + mdat)
    return path


# ---------------------------------------------------------------- MPG ------

def make_mpg(path: Path | str, *, seconds: float = 2.0) -> Path:
    """Write a minimal MPEG-1 program stream (pack + system + video headers)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = bytearray()
    packs = max(1, int(seconds * 10))
    for index in range(packs):
        index * 3600
        header = (b"\x00\x00\x01\xba" + bytes([
            0x44, 0x00, 0x04, 0x00, 0x04, 0x01,
            0x00, 0x89, 0xC3, 0xF8,
        ]) + struct.pack(">I", (0 << 30) | (90 << 15) | 90))
        out += header
        out += b"\x00\x00\x01\xe0" + struct.pack(">I", 0 << 30 | 15 << 15 | 15) + b"\x00" * 8
        out += b"\x00\x00\x01\xc0" + struct.pack(">I", 0 << 30 | 15 << 15 | 15) + b"\x00" * 8
        out += b"\x00" * 16
    path.write_bytes(bytes(out))
    return path


# ------------------------------------------------------------- episode -----

DEFAULT_METADATA = """TITLE=THOT – Der Schlüssel zum Bewusstsein
DESCRIPTION=Eine Reise in die verborgenen Zusammenhänge von Bewusstsein, Herz,
Gehirn und antiker Spiritualität.
Diese zweite Zeile gehört weiterhin zur Beschreibung.

SEASON=1
EPISODE=4
EXPLICIT=false
PUBLISH=NOW
CHAPTERS=
00:00 Intro
02:15 Das vergessene Wissen
07:42 Herz und Bewusstsein
"""


#: Characters that must never appear in a generated file name.
_BAD_NAME_CHARS = set('/\\\0<>:"|?*')


def safe_base_name(name: str) -> str:
    """Reduce *name* to a single, harmless file-name component.

    ``base_name`` is turned straight into a path, so ``"../../escape"`` would
    otherwise write outside the folder the caller asked for. Windows also
    forbids ``<>:"|?*`` and a trailing dot or space.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("The episode name must not be empty.")
    cleaned = "".join("_" if ch in _BAD_NAME_CHARS else ch for ch in name)
    cleaned = cleaned.strip(". ")
    if not cleaned or set(cleaned) <= {".", " "}:
        raise ValueError(f"{name!r} is not a usable file name.")
    if len(cleaned) > 120:
        # Leave room for the suffix and stay inside Windows' 260-char limit.
        cleaned = cleaned[:120].rstrip(". ")
    return cleaned


def _with_title(metadata: str, base_name: str) -> str:
    """Make the sample's TITLE line agree with the file name.

    Without this, a pair called "Demo Episode.mp3" / "Demo Episode.txt" would
    publish under the sample's original title, which is confusing when a user
    is checking that pairing works.
    """
    title = base_name.replace("_", " ").strip()
    lines = metadata.splitlines()
    if not lines:
        return metadata
    if lines[0].upper().startswith("TITLE="):
        lines[0] = f"TITLE={title}"
    else:
        lines.insert(0, f"TITLE={title}")
    return "\n".join(lines) + ("\n" if metadata.endswith("\n") else "")


def write_test_episode(
    incoming: Path | str,
    base_name: str = "THOT_Der_Schluessel",
    *,
    media: str = "mp4",
    metadata: str | None = DEFAULT_METADATA,
    seconds: float = 3.0,
) -> tuple[Path, Path | None]:
    """Write a media file plus its metadata file into *incoming*."""
    incoming = Path(incoming)
    base_name = safe_base_name(base_name)
    incoming.mkdir(parents=True, exist_ok=True)
    media_path = {
        "mp4": lambda: make_mp4(incoming / f"{base_name}.mp4", seconds=seconds),
        "mov": lambda: make_mp4(incoming / f"{base_name}.mov", seconds=seconds,
                                 container="qt"),
        "wav": lambda: make_wav(incoming / f"{base_name}.wav", seconds=seconds),
        "mp3": lambda: make_mp3(incoming / f"{base_name}.mp3", seconds=seconds),
        "mpg": lambda: make_mpg(incoming / f"{base_name}.mpg", seconds=seconds),
    }.get(media)
    if media_path is None:
        raise ValueError(f"Unsupported test media type {media!r}. Use one of {SUPPORTED_FIXTURES}.")
    media_file = media_path()
    meta_file = None
    if metadata is not None:
        meta_file = incoming / f"{base_name}.txt"
        meta_file.write_text(_with_title(metadata, base_name), encoding="utf-8")
    return media_file, meta_file


def write_slow_copy(source: Path, target: Path, *, chunks: int = 5,
                    delay: float = 0.05) -> None:
    """Copy a file slowly in chunks - for stability-detection tests."""
    import time

    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    step = max(1, len(data) // chunks)
    with open(target, "wb") as fh:
        for offset in range(0, len(data), step):
            fh.write(data[offset:offset + step])
            fh.flush()
            time.sleep(delay)
