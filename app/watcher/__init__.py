"""Filesystem watching: stability detection, pairing, and the watch loop."""

from .pairing import EpisodePair, MediaCandidate, MetadataCandidate, pair_directory
from .stability import StabilityTracker, is_file_locked, wait_until_stable
from .filesystem_watcher import FilesystemWatcher, WatcherEvent, WatcherStatus

__all__ = [
    "EpisodePair",
    "MediaCandidate",
    "MetadataCandidate",
    "pair_directory",
    "StabilityTracker",
    "is_file_locked",
    "wait_until_stable",
    "FilesystemWatcher",
    "WatcherEvent",
    "WatcherStatus",
]
