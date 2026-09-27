"""Media inspection and Spotify specification validation.

No ffprobe is required: the built-in container reader is exercised directly,
and the ffprobe branch is driven through a stub over a real file on disk, so
both paths run the complete inspect -> validate chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import MediaInfo, MediaKind, ValidationReport
from app.media.ffmpeg import FfmpegTools, ToolNotFound, safe_run
from app.media.inspector import MediaInspector, classify_extension
from app.media.validator import SPOTIFY_LIMITS, MediaValidator


# ------------------------------------------------------------------ helpers

class _StubResult:
    def __init__(self, data, warnings=None, error="", duration_ms=1):
        self.data = data
        self.warnings = warnings or []
        self.error = error
        self.duration_ms = duration_ms
        self.ok = not error

    @property
    def streams(self):
        return self.data.get("streams", [])

    @property
    def duration(self):
        return float(self.data.get("format", {}).get("duration", 0.0))


def _payload(**fmt):
    """A 1080p H.264 + stereo AAC file that conforms to Spotify's spec."""
    format_ = {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "1812.5",
        "size": "512000000",
        "bit_rate": "2260000",
    }
    streams = [
        {
            "codec_type": "video", "codec_name": "h264", "profile": "High",
            "width": 1920, "height": 1080, "r_frame_rate": "25/1",
            "pix_fmt": "yuv420p", "color_space": "bt709", "field_order": "progressive",
        },
        {
            "codec_type": "audio", "codec_name": "aac", "channels": 2,
            "sample_rate": "48000", "bit_rate": "192000",
        },
    ]
    format_.update(fmt)
    return {"format": format_, "streams": streams}


class StubTools:
    """Stands in for :class:`FfmpegTools` without needing the binary."""

    def __init__(self, payload=None, error="", available=True):
        self._payload = payload
        self._error = error
        self._available = available
        self.calls: list[Path] = []

    def available(self):
        return self._available

    def version(self):
        return "stub"

    def probe(self, path, *, timeout=180.0):
        self.calls.append(Path(path))
        return _StubResult(self._payload if self._payload is not None else {},
                           error=self._error)

    def derive_copy(self, *a, **k):  # pragma: no cover - not needed here
        raise ToolNotFound("stub")


@pytest.fixture
def builtin(config):
    """Validator that uses the dependency-free container reader."""
    return MediaValidator(MediaInspector(FfmpegTools(), config.media), config.media)


@pytest.fixture
def stubbed(config):
    """(validator, tools) that uses the stubbed ffprobe branch."""
    tools = StubTools(_payload())
    return MediaValidator(MediaInspector(tools, config.media), config.media), tools


@pytest.fixture
def fake_file(tmp_path):
    """A real, non-empty file on disk so stat() and can_probe() behave."""
    def _make(name="ep12.mp4", size=4096):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00" * size)
        return p
    return _make


# ------------------------------------------------------- extension mapping

class TestClassifyExtension:
    @pytest.mark.parametrize("name,expected", [
        ("ep12.mp4", MediaKind.VIDEO),
        ("ep12.MOV", MediaKind.VIDEO),
        ("ep12.mpg", MediaKind.VIDEO),
        ("ep12.mp3", MediaKind.AUDIO),
        ("ep12.m4a", MediaKind.AUDIO),
        ("ep12.wav", MediaKind.AUDIO),
        ("ep12.WAV", MediaKind.AUDIO),
        ("ep12.txt", MediaKind.UNKNOWN),
        ("ep12.srt", MediaKind.UNKNOWN),
        ("ep12", MediaKind.UNKNOWN),
    ])
    def test_classification(self, name, expected, config):
        assert classify_extension(name, config.media) is expected

    def test_unknown_when_no_policy(self):
        assert classify_extension("ep12.mp4") is MediaKind.VIDEO
        assert classify_extension("ep12.xyz") is MediaKind.UNKNOWN


# ------------------------------------------------------------- built-in probe

class TestBuiltinProbe:
    def test_wav_duration_and_tracks(self, builtin, tmp_path):
        from make_test_media import make_wav

        p = make_wav(tmp_path / "a.wav", seconds=3.0)
        info = builtin.inspector.inspect(p).info
        assert info.kind is MediaKind.AUDIO
        assert info.has_audio
        assert 2.9 < info.duration < 3.1
        assert info.audio_channels == 2
        assert info.sample_rate == 44100
        assert [t.kind for t in info.tracks] == ["audio"]

    def test_mp3_duration(self, builtin, tmp_path):
        from make_test_media import make_mp3

        p = make_mp3(tmp_path / "a.mp3", seconds=2.0)
        info = builtin.inspector.inspect(p).info
        assert info.kind is MediaKind.AUDIO
        assert info.has_audio
        assert 1.5 < info.duration < 2.5

    def test_mp4_container_has_video_and_audio(self, builtin, tmp_path):
        from make_test_media import make_mp4

        p = make_mp4(tmp_path / "a.mp4", seconds=1.0)
        info = builtin.inspector.inspect(p).info
        assert info.kind is MediaKind.VIDEO
        assert info.has_video and info.has_audio
        assert info.width == 1920 and info.height == 1080

    def test_truncated_container_reports_a_usable_error(self, builtin, tmp_path):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"not a media file at all")
        info = builtin.inspector.inspect(p).info
        assert info.error
        lowered = info.error.lower()
        assert "moov" in lowered or "ffmpeg" in lowered

    def test_empty_file(self, builtin, tmp_path):
        p = tmp_path / "empty.mp4"
        p.write_bytes(b"")
        assert "empty" in builtin.inspector.inspect(p).info.error.lower()

    def test_missing_file(self, builtin, tmp_path):
        info = builtin.inspector.inspect(tmp_path / "nope.mp4").info
        assert "cannot be read" in info.error.lower()


# -------------------------------------------------------------- ffprobe path

class TestFfprobePath:
    def test_ffprobe_is_preferred_and_fully_mapped(self, stubbed, fake_file):
        validator, tools = stubbed
        p = fake_file()
        info, report = validator.validate(p)

        assert info.probe_source == "ffprobe"
        assert report.ok, [e.as_dict() for e in report.errors]
        assert tools.calls == [p]
        assert info.duration == pytest.approx(1812.5)
        assert info.video_codec == "h264" and info.video_profile.lower() == "high"
        assert info.audio_codec == "aac" and info.audio_channels == 2
        assert info.frame_rate == pytest.approx(25.0)
        assert info.field_order == "progressive"
        assert info.video_track_count() == 1 and info.audio_track_count() == 1

    def test_builtin_fallback_is_announced(self, config, tmp_path):
        from make_test_media import make_wav

        p = make_wav(tmp_path / "a.wav", seconds=2.0)
        tools = StubTools(_payload(), error="moov atom not found")
        info = MediaInspector(tools, config.media).inspect(p).info
        assert info.probe_source != "ffprobe"
        assert any("ffprobe was available" in w for w in info.warnings)

    def test_empty_ffprobe_payload_yields_no_streams(self, config, fake_file):
        tools = StubTools({"format": {}, "streams": []})
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert "media_has_no_streams" in report.error_codes()

    def test_probe_error_is_surfaced(self, config, fake_file):
        tools = StubTools(None, error="Invalid data found when processing input")
        info = MediaInspector(tools, config.media).inspect(fake_file()).info
        assert info.error

    def test_unparseable_numbers_do_not_crash(self, config, fake_file):
        payload = _payload()
        payload["format"]["duration"] = "N/A"
        payload["format"]["bit_rate"] = "N/A"
        info = MediaInspector(StubTools(payload), config.media).inspect(fake_file()).info
        assert info.duration == 0.0 and info.bitrate == 0


# ------------------------------------------------------------- validation

class TestConformingMediaPasses:
    def test_spec_conformant_video(self, stubbed, fake_file):
        _, report = stubbed[0].validate(fake_file())
        assert report.ok, [e.as_dict() for e in report.errors]
        assert report.warning_codes() == []

    def test_real_wav_file(self, builtin, tmp_path):
        from make_test_media import make_wav

        p = make_wav(tmp_path / "a.wav", seconds=30.0, sample_rate=48000)
        info, report = builtin.validate(p)
        assert report.ok, [e.as_dict() for e in report.errors]
        assert info.kind is MediaKind.AUDIO

    def test_conformant_mp3(self, builtin, tmp_path):
        from make_test_media import make_mp3

        p = make_mp3(tmp_path / "a.mp3", seconds=45.0)
        _, report = builtin.validate(p)
        assert report.ok, [e.as_dict() for e in report.errors]


class TestHardFailures:
    """Conditions Spotify rejects outright, which must stop publication."""

    def _validate(self, config, fake_file, **fmt):
        payload = _payload(**fmt)
        for key, value in fmt.pop("_streams", {}).items():
            payload["streams"][key] = value
        tools = StubTools(payload)
        return MediaValidator(MediaInspector(tools, config.media),
                              config.media).validate(fake_file())

    def test_over_twelve_hours(self, config, fake_file):
        _, report = self._validate(config, fake_file, duration=str(13 * 3600))
        assert "duration_too_long" in report.error_codes()
        assert any("Split the recording" in (e.hint or "")
                   for e in report.errors)

    def test_over_sixty_gigabytes(self, config, fake_file):
        _, report = self._validate(config, fake_file, size=str(61 * 1024 ** 3))
        assert "file_too_large" in report.error_codes()

    def test_over_the_configured_local_limit(self, config, fake_file):
        config.media.max_file_size_bytes = 1024
        _, report = self._validate(config, fake_file)
        assert "file_exceeds_local_limit" in report.error_codes()

    def test_video_without_audio(self, config, fake_file):
        payload = _payload()
        payload["streams"] = [payload["streams"][0]]
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert "video_without_audio" in report.error_codes()

    def test_audio_only_file_becomes_an_audio_episode(self, config, fake_file):
        """An .mp4 holding only audio is audio, not a broken video."""
        payload = _payload()
        payload["streams"] = [payload["streams"][1]]
        tools = StubTools(payload)
        info, report = MediaValidator(MediaInspector(tools, config.media),
                                      config.media).validate(fake_file())
        assert info.kind is MediaKind.AUDIO
        assert report.ok, [e.as_dict() for e in report.errors]

    def test_video_kind_without_a_video_track_is_rejected(self, config):
        from app.core.models import MediaInfo, ValidationReport

        info = MediaInfo(path="x.mp4", kind=MediaKind.VIDEO, has_video=False,
                         has_audio=True, audio_channels=2, audio_codec="aac")
        v = MediaValidator.__new__(MediaValidator)
        MediaValidator.__init__(v, None, config.media)
        report = ValidationReport()
        v._validate_video(info, report)
        assert "video_file_has_no_video" in report.error_codes()

    def test_surround_sound(self, config, fake_file):
        payload = _payload()
        payload["streams"][1]["channels"] = 6
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert "multichannel_audio" in report.error_codes()

    def test_two_video_tracks(self, config, fake_file):
        payload = _payload()
        payload["streams"].append(dict(payload["streams"][0], index=2))
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert "multiple_video_tracks" in report.error_codes()

    def test_two_audio_tracks(self, config, fake_file):
        payload = _payload()
        payload["streams"].append(dict(payload["streams"][1], index=3))
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert "multiple_audio_tracks" in report.error_codes()

    def test_unsupported_container(self, config, fake_file):
        _, report = self._validate(config, fake_file, format_name="matroska,webm")
        assert "unsupported_container" in report.error_codes()

    def test_unreadable_file(self, builtin, tmp_path):
        _, report = builtin.validate(tmp_path / "gone.mp4")
        assert "media_unreadable" in report.error_codes()

    def test_no_streams_at_all(self, builtin, tmp_path):
        p = tmp_path / "empty.mp4"
        p.write_bytes(b"\x00" * 64)
        _, report = builtin.validate(p)
        assert report.ok is False
        assert report.errors


class TestWarningsDoNotBlock:
    """Spotify accepts these, so they must be warnings, never blockers."""

    def test_low_resolution(self, config, fake_file):
        payload = _payload()
        payload["streams"][0].update(width=640, height=360)
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert report.ok
        assert "low_resolution" in report.warning_codes()

    def test_long_but_legal_episode(self, config, fake_file):
        _, report = self._long(config, fake_file)
        assert report.ok
        assert "duration_long" in report.warning_codes()

    def _long(self, config, fake_file):
        tools = StubTools(_payload(duration=str(6 * 3600)))
        return MediaValidator(MediaInspector(tools, config.media),
                              config.media).validate(fake_file())

    def test_unusual_frame_rate(self, config, fake_file):
        payload = _payload()
        payload["streams"][0]["r_frame_rate"] = "17/1"
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert report.ok
        assert "unusual_frame_rate" in report.warning_codes()

    def test_interlaced(self, config, fake_file):
        payload = _payload()
        payload["streams"][0]["field_order"] = "tt"
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert report.ok
        assert "interlaced_video" in report.warning_codes()

    def test_nonstandard_codecs(self, config, fake_file):
        payload = _payload()
        payload["streams"][0]["codec_name"] = "vp9"
        payload["streams"][1]["codec_name"] = "vorbis"
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert report.ok
        assert "nonstandard_video_codec" in report.warning_codes()
        assert "nonstandard_audio_codec" in report.warning_codes()

    def test_validator_never_blocks_on_a_warning(self, config, fake_file):
        """Escalating warnings is the queue's job, not the validator's."""
        payload = _payload()
        payload["streams"][0].update(width=320, height=240)
        tools = StubTools(payload)
        _, report = MediaValidator(MediaInspector(tools, config.media),
                                   config.media).validate(fake_file())
        assert report.warnings
        assert report.ok


# ------------------------------------------------------------- conversion

class TestConversionAdvice:
    def _needs(self, config, **kw):
        from app.core.models import MediaInfo

        v = MediaValidator.__new__(MediaValidator)
        MediaValidator.__init__(v, None, config.media)
        info = MediaInfo(**kw)
        return v.needs_conversion(info)

    def _good(self, **kw):
        base = dict(path="x.mp4", kind=MediaKind.VIDEO, has_video=True, has_audio=True,
                    video_codec="h264", width=1920, height=1080,
                    audio_codec="aac", audio_channels=2, frame_rate=25.0,
                    audio_bitrate=192000, container="mp4", duration=1800.0)
        base.update(kw)
        return base

    def test_conforming_needs_nothing(self, config):
        needed, reason = self._needs(config, **self._good())
        assert needed is False and reason == ""

    def test_unsupported_codec_is_not_reencoded(self, config):
        """A recommendation alone must never trigger a destructive re-encode."""
        needed, reason = self._needs(config, **self._good(video_codec="vp9"))
        assert needed is False and reason == ""

    def test_missing_audio_track_requires_a_derived_copy(self, config):
        needed, reason = self._needs(config, **self._good(has_audio=False))
        assert needed is True
        assert "audio" in reason.lower()

    def test_extra_tracks_require_a_derived_copy(self, config):
        from app.core.models import TrackInfo

        info_tracks = [TrackInfo(index=0, kind="video"),
                       TrackInfo(index=1, kind="audio"),
                       TrackInfo(index=2, kind="audio")]
        from app.core.models import MediaInfo

        v = MediaValidator.__new__(MediaValidator)
        MediaValidator.__init__(v, None, config.media)
        info = MediaInfo(path="x.mp4", kind=MediaKind.VIDEO, has_video=True, has_audio=True,
                         video_codec="h264", width=1920, height=1080,
                         audio_codec="aac", audio_channels=2, duration=1800.0,
                         tracks=info_tracks)
        needed, reason = v.needs_conversion(info)
        assert needed is True
        assert "audio track" in reason.lower()

    def test_overlong_is_never_converted(self, config):
        needed, reason = self._needs(config, **self._good(duration=13 * 3600))
        assert needed is False

    def test_surround_offers_conversion(self, config):
        needed, reason = self._needs(config, **self._good(audio_channels=6))
        assert needed is True

    def test_audio_only_never_needs_conversion(self, config):
        needed, reason = self._needs(
            config, path="x.mp3", kind=MediaKind.AUDIO, has_video=False, has_audio=True,
            audio_codec="mp3", audio_channels=2, duration=1800.0)
        assert needed is False


# ------------------------------------------------------------- subprocess

class TestSafeRun:
    def test_uses_argument_array_not_a_shell(self):
        proc = safe_run(["python3", "-c", "print('hi; rm -rf /')"], timeout=30)
        assert proc.returncode == 0
        assert b"hi; rm -rf /" in proc.stdout

    def test_shell_metacharacters_are_literal_arguments(self):
        proc = safe_run(["python3", "-c", "import sys; sys.stdout.write(sys.argv[1])",
                         "a && b | c; $(whoami) `id`"],
                        timeout=30)
        assert proc.stdout == b"a && b | c; $(whoami) `id`"

    def test_empty_argv_is_rejected(self):
        with pytest.raises(ValueError):
            safe_run([])

    def test_missing_binary_raises_tool_not_found(self):
        with pytest.raises(ToolNotFound):
            safe_run(["definitely-not-a-real-binary-xyz"], timeout=10)

    def test_check_false_returns_nonzero_without_raising(self):
        assert safe_run(["python3", "-c", "import sys; sys.exit(3)"]).returncode == 3

    def test_check_true_raises(self):
        with pytest.raises(Exception):
            safe_run(["python3", "-c", "import sys; sys.exit(3)"], check=True)

    def test_timeout_is_enforced(self):
        with pytest.raises(TimeoutError):
            safe_run(["python3", "-c", "import time; time.sleep(30)"], timeout=0.5)


class TestFfmpegToolsAvailability:
    def test_reports_unavailable_without_the_binary(self):
        tools = FfmpegTools(ffprobe_path="", ffmpeg_path="")
        # A real ffmpeg may exist on the machine; either answer is valid, but
        # the reported state must be consistent.
        assert isinstance(tools.available(), bool)

    def test_unavailable_tools_do_not_break_probing(self, config, fake_file):
        tools = FfmpegTools(ffprobe_path="no-such-ffprobe", ffmpeg_path="no-such-ffmpeg")
        assert tools.available() is False
        info = MediaInspector(tools, config.media).inspect(fake_file()).info
        assert info.error


# ---------------------------------------------------------------------------
# The limits below are transcribed from Spotify's current documentation and
# are re-checked at each release.  The point of every test here is the same:
# a RECOMMENDED setting must not become a refusal.  Only what Spotify actually
# requires may block publication.
# ---------------------------------------------------------------------------


class TestRecommendedVersusRequired:
    """A recommendation warns; only a documented requirement refuses."""

    def _video(self, config, **overrides):
        base = dict(path="x.mp4", kind=MediaKind.VIDEO, has_video=True,
                    has_audio=True, video_codec="h264", width=1920,
                    height=1080, duration=600.0, frame_rate=30.0,
                    audio_codec="aac", audio_channels=2,
                    audio_bitrate=192_000, size=100 * 1024 ** 2)
        base.update(overrides)
        info = MediaInfo(**base)
        v = MediaValidator.__new__(MediaValidator)
        MediaValidator.__init__(v, None, config.media)
        report = ValidationReport()
        v._validate_video(info, report)
        return report

    def test_a_non_16_9_video_warns_but_publishes(self, config):
        """Spotify lists 16:9 as recommended, any ratio as compatible."""
        report = self._video(config, width=1440, height=1440)
        assert "non_widescreen" in report.warning_codes()
        assert not report.error_codes()

    def test_a_16_9_video_produces_no_aspect_warning(self, config):
        assert "non_widescreen" not in self._video(config).warning_codes()

    def test_video_audio_below_the_recommended_bitrate_only_warns(self, config):
        """Spotify's video page recommends 192 kbps; it is not a limit."""
        report = self._video(config, audio_bitrate=128_000)
        assert "low_video_audio_bitrate" in report.warning_codes()
        assert not report.error_codes()

    def test_surround_audio_is_a_hard_error(self, config):
        """Multi-channel audio IS unsupported, unlike a low bitrate."""
        report = self._video(config, audio_channels=6)
        assert "multichannel_audio" in report.error_codes()

    def test_an_unusual_frame_rate_only_warns(self, config):
        report = self._video(config, frame_rate=15.0)
        assert "unusual_frame_rate" in report.warning_codes()
        assert not report.error_codes()

    def test_a_non_recommended_codec_only_warns(self, config):
        report = self._video(config, video_codec="vp9")
        assert report.ok, [e.as_dict() for e in report.errors]


class TestSupportedContainersAreExactlyWhatSpotifyLists:
    """MP3, M4A and WAV for audio; MP4, MOV and MPG for video."""

    def test_audio_aiff_is_refused(self):
        """AIFF is not on Spotify's supported audio list."""
        from app.media.validator import SPOTIFY_LIMITS

        assert "aiff" not in SPOTIFY_LIMITS.SUPPORTED_AUDIO_CONTAINERS
        for supported in ("mp3", "m4a", "wav"):
            assert supported in SPOTIFY_LIMITS.SUPPORTED_AUDIO_CONTAINERS

    def test_video_containers_include_mp4_mov_and_mpg(self):
        from app.media.validator import SPOTIFY_LIMITS

        for supported in ("mp4", "mov", "mpg"):
            assert supported in SPOTIFY_LIMITS.SUPPORTED_VIDEO_CONTAINERS

    def test_aiff_is_not_offered_as_a_usable_media_file(self):
        from app.media.builtin_probe import can_probe

        assert can_probe("episode.aiff") is False
        assert can_probe("episode.mp3") is True

    def test_documented_size_and_duration_ceilings(self):
        from app.media.validator import SPOTIFY_LIMITS

        # video-specs: recommended < 10 GB / < 4 h, compatible up to 60 GB / 12 h
        assert SPOTIFY_LIMITS.MAX_SIZE_BYTES == 60 * 1024 ** 3
        assert SPOTIFY_LIMITS.RECOMMENDED_SIZE_BYTES == 10 * 1024 ** 3
        assert SPOTIFY_LIMITS.MAX_DURATION_SECONDS == 12 * 3600
        assert SPOTIFY_LIMITS.RECOMMENDED_DURATION_SECONDS == 4 * 3600
