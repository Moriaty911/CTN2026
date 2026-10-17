"""Platform client boundary and adapters (`MockLegitClient`, `OfficialLegitClient`)."""

from .base import AccountSuspended, AuthError, LegitClient, PlatformError, Throttled
from .legit_v1 import NormalizedPage, OfficialLegitClient, extract_hashtags, parse_timestamp
from .mock import MockLegitClient

__all__ = [
    "AccountSuspended",
    "AuthError",
    "LegitClient",
    "MockLegitClient",
    "NormalizedPage",
    "OfficialLegitClient",
    "PlatformError",
    "Throttled",
    "extract_hashtags",
    "parse_timestamp",
]
