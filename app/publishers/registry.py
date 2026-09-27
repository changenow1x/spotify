"""Adapter registry.

The core application only ever asks for an adapter *by name*.  Nothing imports
``publishers.spotify`` directly, so a new platform is added by writing one
module and registering it here (or in ``config.yaml``) - never by editing the
queue, the database or the dashboard.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterator

from .base import HealthCheck, PublisherAdapter

log = logging.getLogger(__name__)

__all__ = ["Registry", "get_registry", "register_adapter", "known_adapters"]


class Registry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], PublisherAdapter]] = {}
        self._instances: dict[str, PublisherAdapter] = {}

    def register(self, name: str, factory: Callable[[], PublisherAdapter]) -> None:
        name = name.strip().lower()
        if name in self._factories:
            log.debug("Replacing already-registered adapter %s", name)
        self._factories[name] = factory
        self._instances.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._factories)

    def has(self, name: str) -> bool:
        return name.strip().lower() in self._factories

    def get(self, name: str) -> PublisherAdapter:
        key = name.strip().lower()
        if key not in self._factories:
            raise KeyError(
                f"No publishing adapter named {name!r}. Available: {', '.join(self.names()) or '(none)'}"
            )
        instance = self._instances.get(key)
        if instance is None:
            instance = self._factories[key]()
            self._instances[key] = instance
        return instance

    def close_all(self) -> None:
        for name, instance in list(self._instances.items()):
            try:
                instance.close()
            except Exception:  # noqa: BLE001  # pragma: no cover
                log.debug("Error closing adapter %s", name, exc_info=True)
        self._instances.clear()

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def health(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name in self.names():
            try:
                adapter = self.get(name)
                out[name] = {
                    "display_name": getattr(adapter, "display_name", name),
                    "status": adapter.get_status().as_dict(),
                    "capabilities": adapter.capabilities().as_dict(),
                }
            except Exception as exc:  # noqa: BLE001
                out[name] = {
                    "display_name": name,
                    "status": HealthCheck(False, f"Adapter could not be created: {exc}").as_dict(),
                    "capabilities": {},
                }
        return out


_REGISTRY = Registry()


def get_registry() -> Registry:
    return _REGISTRY


def register_adapter(name: str, factory: Callable[[], PublisherAdapter]) -> None:
    _REGISTRY.register(name, factory)


def known_adapters() -> list[dict]:
    """Built-in adapters plus the documented future candidates.

    The future candidates are listed so the dashboard can explain *why* they are
    not available instead of the user wondering whether the app supports them.
    """
    return [
        {
            "name": "spotify",
            "status": "implemented",
            "note": "Audio and video episodes, immediate and scheduled publication.",
        },
        {
            "name": "apple_podcasts",
            "status": "future_candidate",
            "note": (
                "No official write API for episode upload; requires an RSS feed "
                "hosted elsewhere. Would mean a hosting integration rather than a "
                "file drop, so it is out of scope for the local folder workflow."
            ),
        },
        {
            "name": "amazon_music",
            "status": "future_candidate",
            "note": (
                "Submission is RSS-based and additionally requires a manual "
                "Amazon Music for Podcasters account approval. Fails the "
                "'no recurring manual step' requirement."
            ),
        },
        {
            "name": "youtube",
            "status": "future_candidate",
            "note": (
                "The official Data API is read-only; uploads require either the "
                "Studio UI (manual) or an unapproved Content ID flow. Adding it "
                "would mean bypassing security controls."
            ),
        },
    ]
