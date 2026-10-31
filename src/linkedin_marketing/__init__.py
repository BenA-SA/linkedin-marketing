"""Unofficial Python client for the official LinkedIn Marketing API.

Not affiliated with, endorsed by, or supported by LinkedIn or Microsoft.
"""

from linkedin_marketing.client import LinkedInClient
from linkedin_marketing.config import Settings
from linkedin_marketing.errors import (
    ApiError,
    AuthError,
    ConfigError,
    LinkedInError,
    ModuleDisabledError,
    NotAllowedError,
    RateLimitError,
    WriteRefusedError,
)
from linkedin_marketing.hashing import hash_email
from linkedin_marketing.modules import MODULES
from linkedin_marketing.resources.audiences import AudienceCompany, AudienceUser
from linkedin_marketing.resources.conversions import ConversionEvent
from linkedin_marketing.safety import WriteResult

__version__ = "0.1.0"

__all__ = [
    "MODULES",
    "ApiError",
    "AudienceCompany",
    "AudienceUser",
    "AuthError",
    "ConfigError",
    "ConversionEvent",
    "LinkedInClient",
    "LinkedInError",
    "ModuleDisabledError",
    "NotAllowedError",
    "RateLimitError",
    "Settings",
    "WriteRefusedError",
    "WriteResult",
    "hash_email",
]
