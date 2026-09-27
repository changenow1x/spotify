"""Media validation against Spotify's published specifications.

The distinction that matters, and that the docs support directly:

**errors** - Spotify documents these as unsupported or hard limits.  Uploading
would fail or produce a broken episode, so the job stops.
**warnings** - Spotify *recommends* better settings.  The file will work, so
the episode is published but the deviation is reported clearly.

Sources (spotify.com/us/creators, "Video specs" and "Publishing videos"):
  - one video track + one audio track, same duration
  - duration <= 12 h
  - containers MP4 / MOV / MPG
  - video: H.264 / H.265 recommended, 16:9, 24/25/30/50/60 fps, progressive
  - audio: AAC-LC/PCM/FLAC recommended, 128 kbps+, stereo; surround
    (5.1) is downmixed and multi-channel is not supported
  - recommended < 10 GB, compatible up to 60 GB
"""

from __future__ import annotations

from pathlib import Path

from ..core.models import MediaInfo, MediaKind, ValidationReport
from .inspector import MediaInspector

__all__ = ["MediaValidator", "SPOTIFY_LIMITS"]


class SpotifyMediaLimits:
    """Numbers transcribed from Spotify's current documentation."""

    MAX_DURATION_SECONDS = 12 * 3600
    RECOMMENDED_DURATION_SECONDS = 4 * 3600
    MAX_SIZE_BYTES = 60 * 1024 ** 3
    RECOMMENDED_SIZE_BYTES = 10 * 1024 ** 3
    SUPPORTED_VIDEO_CONTAINERS = {"mp4", "mov", "mpeg", "mpg", "m4v", "qt"}
    #: Spotify's audio page lists MP3, M4A and WAV.  The extra names are
    #: container strings ffprobe reports for those same files; no other audio
    #: format is accepted (AIFF, for instance, is not on the list).
    SUPPORTED_AUDIO_CONTAINERS = {"mp3", "m4a", "mp4a", "wav", "wave", "mov,mp4,m4a"}
    RECOMMENDED_VIDEO_CODECS = {"h264", "hevc", "h.264", "h.265"}
    RECOMMENDED_AUDIO_CODECS = {"aac", "mp3", "pcm_s16le", "pcm", "flac", "alac", "wav", "aac_latm"}
    RECOMMENDED_FRAME_RATES = {24.0, 25.0, 30.0, 50.0, 60.0,
                               23.976, 29.97, 59.94, 48.0}
    MIN_AUDIO_BITRATE = 128_000
    RECOMMENDED_VIDEO_AUDIO_BITRATE = 192_000
    RECOMMENDED_MIN_WIDTH = 1280
    RECOMMENDED_MIN_HEIGHT = 720
    #: 16:9 with a little tolerance for 4:3 / 1:1 sources.
    ASPECT_RATIO = 16 / 9


SPOTIFY_LIMITS = SpotifyMediaLimits


class MediaValidator:
    def __init__(self, inspector: MediaInspector, policy):
        self.inspector = inspector
        self.policy = policy

    @staticmethod
    def _looks_truncated(info: MediaInfo) -> bool:
        """True when the file is far smaller than its own header claims.

        Only applied when the bitrate is a real constant one: for a variable
        bitrate the average bitrate is not a reliable predictor, and false
        alarms here would block legitimate uploads.
        """
        duration = info.duration
        size = info.size or 0
        bitrate = info.audio_bitrate or 0
        if duration <= 5 or size <= 0 or bitrate <= 0:
            return False
        if info.container not in {"mp3", "aac", "m4a", "wav", "wave"}:
            return False
        # Header and container overhead is a few percent; 60% is far beyond
        # anything a complete file would show.
        return size < 0.40 * (bitrate / 8.0) * duration

    def validate(self, path: Path | str) -> tuple[MediaInfo, ValidationReport]:
        """Inspect *path* and judge it against Spotify's specification."""
        result = self.inspector.inspect(path)
        info = result.info
        report = ValidationReport()

        if info.error:
            report.add_error("media_unreadable", info.error, hint=(
                "The file may still be copying, be corrupt, or not be media at all."))
        if not info.has_video and not info.has_audio:
            report.add_error(
                "media_has_no_streams",
                "The file contains no audio and no video, so there is nothing to publish.",
            )
        for warning in info.warnings:
            report.add_warning("media_note", warning)

        kind = info.kind
        if kind is MediaKind.VIDEO:
            self._validate_video(info, report)
        elif kind is MediaKind.AUDIO:
            self._validate_audio(info, report)

        self._validate_common(info, report)
        report.info["probe_source"] = info.probe_source
        report.info["kind"] = kind.value
        report.info["duration"] = info.duration
        report.info["duration_hms"] = info.as_dict().get("duration_hms")
        report.info["size_bytes"] = info.size
        report.ok = not report.errors
        return info, report

    # -- shared ------------------------------------------------------------
    def _validate_common(self, info: MediaInfo, report: ValidationReport) -> None:
        limits = SPOTIFY_LIMITS
        policy = self.policy

        if info.duration <= 0:
            report.add_warning(
                "duration_unknown",
                "The duration could not be read. A 0-second or unreadable "
                "episode will be rejected by Spotify.",
            )
        elif info.duration < 1.0:
            # A sub-second "episode" is always a fragment: a copy that was cut
            # short, or a header with no media behind it.  Publishing it would
            # create a real, broken episode on the user's show.
            report.add_error(
                "duration_implausibly_short",
                f"The file is only {info.duration:.2f} seconds long. It looks "
                f"incomplete or corrupt rather than like a finished recording.",
                hint="Copy the whole recording into the incoming folder again.",
            )
        elif self._looks_truncated(info):
            # A constant-bitrate file states its full length in the header, so
            # cutting the data off still reports the original duration. The
            # only cheap way to see it is to compare the bytes on disk with the
            # bytes the declared bitrate and length imply.
            expected = info.audio_bitrate / 8.0 * info.duration if info.audio_bitrate else 0
            report.add_error(
                "media_appears_truncated",
                f"The file claims to be {info.duration:.0f} seconds long, which at "
                f"{int(info.audio_bitrate or 0) // 1000} kbps needs about "
                f"{expected / 1e6:.1f} MB, but it is only "
                f"{(info.size or 0) / 1e6:.1f} MB. It looks cut short.",
                hint=("The copy did not finish. Copy the whole recording into "
                      "the incoming folder again."),
            )
        elif info.duration > limits.MAX_DURATION_SECONDS:
            report.add_error(
                "duration_too_long",
                f"The episode is {info.duration / 3600:.1f} hours long. Spotify's "
                f"maximum is 12 hours.",
                hint="Split the recording into separate episodes.",
            )
        elif info.duration > limits.RECOMMENDED_DURATION_SECONDS:
            report.add_warning(
                "duration_long",
                f"The episode is {info.duration / 3600:.1f} hours long. Spotify "
                f"recommends under 4 hours because long files are more likely to "
                f"fail mid-upload.",
            )

        if info.size > limits.MAX_SIZE_BYTES:
            report.add_error(
                "file_too_large",
                f"The file is {info.size / 1024 ** 3:.1f} GB. Spotify's documented "
                f"maximum is 60 GB.",
            )
        elif info.size > limits.RECOMMENDED_SIZE_BYTES:
            report.add_warning(
                "file_large",
                f"The file is {info.size / 1024 ** 3:.1f} GB. Spotify recommends "
                f"under 10 GB; larger files take longer to process and are more "
                f"likely to fail if the connection drops.",
            )
        max_policy = getattr(policy, "max_file_size_bytes", 0) if policy else 0
        if max_policy and info.size > max_policy:
            report.add_error(
                "file_exceeds_local_limit",
                f"The file is {info.size / 1024 ** 3:.1f} GB, above the limit of "
                f"{max_policy / 1024 ** 3:.1f} GB configured for this installation.",
            )

    # -- video -------------------------------------------------------------
    def _validate_video(self, info: MediaInfo, report: ValidationReport) -> None:
        limits = SPOTIFY_LIMITS
        if not info.has_audio:
            report.add_error(
                "video_without_audio",
                "The video has no audio track. Spotify requires exactly one "
                "audio track in a video episode.",
                hint="Export the video with sound, or publish it as a separate audio episode.",
            )
        if not info.has_video:
            report.add_error("video_file_has_no_video", "The file has no video track.")

        video_tracks = [t for t in info.tracks if t.kind == "video"]
        audio_tracks = [t for t in info.tracks if t.kind == "audio"]
        if len(video_tracks) > 1:
            report.add_error(
                "multiple_video_tracks",
                f"The file contains {len(video_tracks)} video tracks. Spotify "
                f"requires exactly one.",
                hint="Re-export keeping only the main video track.",
            )
        if len(audio_tracks) > 1:
            report.add_error(
                "multiple_audio_tracks",
                f"The file contains {len(audio_tracks)} audio tracks. Spotify "
                f"requires exactly one audio track (surround audio is not supported).",
                hint="Downmix to a single stereo track before publishing.",
            )
        if len(audio_tracks) == 1 and audio_tracks[0].channels > 2:
            report.add_error(
                "multichannel_audio",
                f"The audio has {audio_tracks[0].channels} channels. Spotify does "
                f"not support multi-channel audio in video episodes.",
                hint="Downmix to stereo.",
            )

        container = (info.container or info.format_name or "").lower()
        if container and not any(c in container for c in limits.SUPPORTED_VIDEO_CONTAINERS):
            report.add_error(
                "unsupported_container",
                f"The container is {container!r}. Spotify supports MP4, MOV and MPG.",
            )

        if info.video_codec and info.video_codec not in limits.RECOMMENDED_VIDEO_CODECS:
            report.add_warning(
                "nonstandard_video_codec",
                f"The video codec is {info.video_codec}. Spotify recommends "
                f"H.264 (or H.265) and accepts many others, but conversion is safer.",
            )
        if info.video_profile == "baseline":
            report.add_warning(
                "video_profile_baseline",
                "The video uses the H.264 Baseline profile. Spotify recommends "
                "High Profile.",
            )
        if info.width and info.width < limits.RECOMMENDED_MIN_WIDTH:
            report.add_warning(
                "low_resolution",
                f"The resolution is {info.resolution_label}. Spotify recommends "
                f"at least 1280x720 and displays higher resolutions best.",
            )
        if info.height and info.height > 2160:
            report.add_warning(
                "very_high_resolution",
                f"The resolution is {info.resolution_label}. Spotify recommends "
                f"1080p for most shows.",
            )
        if info.frame_rate and info.frame_rate > 0:
            nearest = min(limits.RECOMMENDED_FRAME_RATES,
                          key=lambda r: abs(r - info.frame_rate))
            if abs(nearest - info.frame_rate) > 0.5:
                report.add_warning(
                    "unusual_frame_rate",
                    f"The frame rate is {info.frame_rate:g} fps. Spotify "
                    f"recommends 24, 25, 30, 50 or 60 fps and the source's native rate.",
                )
        if info.field_order.lower() in {"tt", "bb", "tb", "bt"}:
            report.add_warning(
                "interlaced_video",
                "The video is interlaced. Spotify recommends progressive scan.",
            )
        # Spotify lists 16:9 as recommended and *any* aspect ratio as
        # compatible, so this is a warning and never a refusal - the episode
        # is published and merely letterboxed or pillarboxed in some apps.
        if info.width and info.height and abs(info.width / info.height - limits.ASPECT_RATIO) > 0.02:
            report.add_warning(
                "non_widescreen",
                f"The video is {info.width}x{info.height}, which is not 16:9 "
                f"widescreen. Spotify recommends 16:9; other aspect ratios are "
                f"accepted but are letterboxed or pillarboxed in some apps.",
            )
        if info.audio_channels > 2:
            report.add_error("multichannel_audio",
                             f"The audio has {info.audio_channels} channels, which Spotify does not support.")

        # Spotify's video page recommends 192 kbps or higher for a video's
        # audio track, while the general audio floor is 128 kbps.  The
        # difference is a recommendation, not a limit, so this stays a warning.
        video_audio_bitrate = (self.policy.min_video_audio_bitrate
                               if self.policy else limits.RECOMMENDED_VIDEO_AUDIO_BITRATE)
        if info.audio_bitrate and info.audio_bitrate < video_audio_bitrate:
            report.add_warning(
                "low_video_audio_bitrate",
                f"The audio bitrate is {info.audio_bitrate // 1000} kbps. Spotify "
                f"recommends {video_audio_bitrate // 1000} kbps or higher for video episodes.",
            )
        if info.audio_codec and info.audio_codec not in limits.RECOMMENDED_AUDIO_CODECS:
            report.add_warning(
                "nonstandard_audio_codec",
                f"The audio codec is {info.audio_codec}. Spotify recommends "
                f"AAC-LC for video episodes.",
            )

    # -- audio -------------------------------------------------------------
    def _validate_audio(self, info: MediaInfo, report: ValidationReport) -> None:
        limits = SPOTIFY_LIMITS
        audio_tracks = [t for t in info.tracks if t.kind == "audio"]
        if len(audio_tracks) > 1:
            report.add_error(
                "multiple_audio_tracks",
                f"The file contains {len(audio_tracks)} audio tracks. Spotify "
                f"expects a single mono or stereo track.",
            )
        if info.audio_channels > 2:
            report.add_error(
                "multichannel_audio",
                f"The audio has {info.audio_channels} channels. Spotify supports "
                f"mono and stereo audio only.",
            )
        if info.has_video:
            report.add_warning(
                "audio_file_contains_video",
                "The audio file also contains a video track. It will be published "
                "as a video episode.",
            )
        min_bitrate = self.policy.min_audio_bitrate if self.policy else limits.MIN_AUDIO_BITRATE
        if info.audio_bitrate and info.audio_bitrate < min_bitrate:
            report.add_warning(
                "low_audio_bitrate",
                f"The audio bitrate is {info.audio_bitrate // 1000} kbps. Spotify "
                f"recommends {min_bitrate // 1000} kbps or higher.",
            )
        if info.audio_codec and info.audio_codec not in limits.RECOMMENDED_AUDIO_CODECS:
            report.add_warning(
                "nonstandard_audio_codec",
                f"The audio codec is {info.audio_codec}. Spotify supports MP3, M4A "
                f"and WAV.",
            )
        if info.sample_rate and info.sample_rate < 44_100:
            report.add_warning(
                "low_sample_rate",
                f"The sample rate is {info.sample_rate} Hz. 44,100 Hz or higher is "
                f"recommended.",
            )

    # -- conversion decision ------------------------------------------------
    def needs_conversion(self, info: MediaInfo) -> tuple[bool, str]:
        """Should we create a derived copy before uploading?

        Only true for things Spotify documents as *unsupported*; recommendations
        alone are reported but do not trigger a re-encode, because re-encoding
        6 GB of user's original material is a destructive-feeling surprise.
        """
        if not info.has_audio and info.has_video:
            return True, "the video has no audio track"
        if not info.has_video and not info.has_audio:
            return False, ""
        if info.has_video and info.audio_channels > 2:
            return True, f"the audio has {info.audio_channels} channels"
        if info.has_video and info.video_track_count() > 1:
            return True, "the file has more than one video track"
        if info.has_video and info.audio_track_count() > 1:
            return True, "the file has more than one audio track"
        if info.duration and info.duration > SPOTIFY_LIMITS.MAX_DURATION_SECONDS:
            return False, "episodes over 12 hours must be split, not converted"
        return False, ""
