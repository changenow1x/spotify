"""Publishing adapters.

Every platform is an independent adapter implementing :class:`PublisherAdapter`.
The core application never imports a platform module directly - it asks the
registry for an adapter by name.  That is what makes "add Apple Podcasts later"
an additive change rather than a rewrite.
"""

from .base import (
    AdapterCapabilities,
    AuthStatus,
    HealthCheck,
    PublishRequest,
    PublishResult,
    PublicationStatus,
    PublisherAdapter,
    PublisherError,
    AuthenticationRequired,
    ConfigurationError,
    RemoteDuplicateFound,
    UploadFailed,
    ValidationFailed,
    NeedsReconciliation,
)
from .registry import Registry, get_registry, register_adapter

__all__ = [
    "AdapterCapabilities",
    "AuthStatus",
    "HealthCheck",
    "PublishRequest",
    "PublishResult",
    "PublicationStatus",
    "PublisherAdapter",
    "PublisherError",
    "AuthenticationRequired",
    "ConfigurationError",
    "RemoteDuplicateFound",
    "UploadFailed",
    "ValidationFailed",
    "NeedsReconciliation",
    "Registry",
    "get_registry",
    "register_adapter",
]
