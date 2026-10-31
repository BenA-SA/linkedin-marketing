"""Settings for the client, built from keyword arguments or the environment.

Secrets (client secret, refresh token, access token) are held only in memory
and are excluded from ``repr``; they never appear in logs or error context.

Environment variables (all optional except credentials):

``LINKEDIN_CLIENT_ID``, ``LINKEDIN_CLIENT_SECRET``, ``LINKEDIN_REFRESH_TOKEN``
    Programmatic refresh-token auth (recommended).
``LINKEDIN_ACCESS_TOKEN``
    A ready access token instead (expires after about 60 days).
``LINKEDIN_API_VERSION``
    ``LinkedIn-Version`` header, ``YYYYMM``. Default ``202610``.
``LINKEDIN_MODULES``
    Comma list of modules to enable. Default ``ads,reporting,ad_library``.
``LINKEDIN_READ_ONLY``
    ``true`` (default) refuses every confirmed write; set ``false`` to allow
    writes, which still need ``confirm=True`` per call.
``LINKEDIN_ALLOWED_AD_ACCOUNT_IDS`` / ``LINKEDIN_ALLOWED_ORGANIZATION_IDS``
    Comma lists. When set, every call naming another account or organization
    is refused before a request is built. Empty means no restriction.
``LINKEDIN_MAX_DAILY_BUDGET``
    Upper bound for daily-budget writes, in the campaign's currency. Budget
    writes are refused while it is unset.
``LINKEDIN_AUDIT_LOG``
    JSONL file for confirmed writes. Default
    ``$XDG_STATE_HOME/linkedin-marketing/writes.jsonl``.
``LINKEDIN_MAX_PAGES``
    Cap on pages fetched by any auto-paging call. Default 20.
``LINKEDIN_RETRY_AFTER_MAX_SECONDS``
    On HTTP 429 with a ``Retry-After`` of at most this many seconds, wait and
    retry once; longer waits raise ``RateLimitError``. Default 10; 0 disables.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from linkedin_marketing.errors import ConfigError
from linkedin_marketing.modules import DEFAULT_MODULES, MODULES

DEFAULT_API_VERSION = "202610"
DEFAULT_MAX_PAGES = 20
DEFAULT_RETRY_AFTER_MAX_SECONDS = 10.0
API_ROOT = "https://api.linkedin.com/rest"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def default_audit_log() -> Path:
    """``$XDG_STATE_HOME/linkedin-marketing/writes.jsonl`` (``~/.local/state`` fallback)."""
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "linkedin-marketing" / "writes.jsonl"


def parse_ids(raw: str, env_name: str) -> tuple[int, ...]:
    """Comma-separated numeric ids; empty input means no allow-list."""
    try:
        return tuple(int(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError([env_name], "Use comma-separated numeric ids.") from exc


def parse_bool(raw: str, env_name: str, default: bool) -> bool:
    """``true/false/1/0/yes/no/on/off`` (case-insensitive); blank gives ``default``."""
    word = raw.strip().lower()
    if not word:
        return default
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError([env_name], "Use true or false.")


def parse_decimal(raw: str, env_name: str) -> Decimal | None:
    """A positive finite decimal, or ``None`` when blank."""
    if not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigError([env_name], "Use a number such as 100.") from exc
    if not value.is_finite() or value <= 0:
        raise ConfigError([env_name], "Use a positive number such as 100.")
    return value


def parse_modules(raw: str | Iterable[str]) -> tuple[str, ...]:
    """Validated module names; ``all`` enables every module."""
    names = raw.split(",") if isinstance(raw, str) else list(raw)
    cleaned = tuple(dict.fromkeys(name.strip().lower() for name in names if name.strip()))
    if "all" in cleaned:
        return tuple(MODULES)
    unknown = [name for name in cleaned if name not in MODULES]
    if unknown:
        raise ConfigError(
            ["LINKEDIN_MODULES"],
            f"Unknown module(s) {', '.join(unknown)}; choose from {', '.join(MODULES)}.",
        )
    return cleaned


@dataclass(frozen=True)
class Settings:
    """Everything the client needs; secrets excluded from ``repr``."""

    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    api_version: str = DEFAULT_API_VERSION
    modules: tuple[str, ...] = DEFAULT_MODULES
    read_only: bool = True
    allowed_ad_account_ids: tuple[int, ...] = ()
    allowed_organization_ids: tuple[int, ...] = ()
    max_daily_budget: Decimal | None = None
    audit_log_path: Path = field(default_factory=default_audit_log)
    max_pages: int = DEFAULT_MAX_PAGES
    retry_after_max_seconds: float = DEFAULT_RETRY_AFTER_MAX_SECONDS

    def __post_init__(self):
        object.__setattr__(self, "modules", parse_modules(self.modules))

    @property
    def uses_refresh_token(self) -> bool:
        """True when a refresh token is configured (preferred over a static token)."""
        return bool(self.refresh_token)

    def missing_credentials(self) -> list[str]:
        """Names (never values) of the credential settings still needed."""
        if self.access_token and not self.refresh_token:
            return []
        named = {
            "LINKEDIN_CLIENT_ID": self.client_id,
            "LINKEDIN_CLIENT_SECRET": self.client_secret,
            "LINKEDIN_REFRESH_TOKEN": self.refresh_token,
        }
        missing = [name for name, value in named.items() if not value]
        if len(missing) == len(named):
            return ["LINKEDIN_REFRESH_TOKEN (with CLIENT_ID/SECRET) or LINKEDIN_ACCESS_TOKEN"]
        return missing

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``environ`` (default ``os.environ``).

        Missing credentials do not raise here, so an MCP server still starts
        and each call can explain what to set.
        """
        env = os.environ if environ is None else environ
        audit = env.get("LINKEDIN_AUDIT_LOG", "").strip()
        max_pages = env.get("LINKEDIN_MAX_PAGES", "").strip()
        return cls(
            client_id=env.get("LINKEDIN_CLIENT_ID", "").strip(),
            client_secret=env.get("LINKEDIN_CLIENT_SECRET", "").strip(),
            refresh_token=env.get("LINKEDIN_REFRESH_TOKEN", "").strip(),
            access_token=env.get("LINKEDIN_ACCESS_TOKEN", "").strip(),
            api_version=env.get("LINKEDIN_API_VERSION", "").strip() or DEFAULT_API_VERSION,
            modules=parse_modules(
                env.get("LINKEDIN_MODULES", "").strip() or ",".join(DEFAULT_MODULES)
            ),
            read_only=parse_bool(env.get("LINKEDIN_READ_ONLY", ""), "LINKEDIN_READ_ONLY", True),
            allowed_ad_account_ids=parse_ids(
                env.get("LINKEDIN_ALLOWED_AD_ACCOUNT_IDS", ""),
                "LINKEDIN_ALLOWED_AD_ACCOUNT_IDS",
            ),
            allowed_organization_ids=parse_ids(
                env.get("LINKEDIN_ALLOWED_ORGANIZATION_IDS", ""),
                "LINKEDIN_ALLOWED_ORGANIZATION_IDS",
            ),
            max_daily_budget=parse_decimal(
                env.get("LINKEDIN_MAX_DAILY_BUDGET", ""), "LINKEDIN_MAX_DAILY_BUDGET"
            ),
            audit_log_path=Path(audit) if audit else default_audit_log(),
            max_pages=_parse_max_pages(max_pages),
            retry_after_max_seconds=_parse_seconds(
                env.get("LINKEDIN_RETRY_AFTER_MAX_SECONDS", "").strip()
            ),
        )


def _parse_max_pages(raw: str) -> int:
    if not raw:
        return DEFAULT_MAX_PAGES
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigError(["LINKEDIN_MAX_PAGES"], "Use a positive whole number.")
    return int(raw)


def _parse_seconds(raw: str) -> float:
    if not raw:
        return DEFAULT_RETRY_AFTER_MAX_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(["LINKEDIN_RETRY_AFTER_MAX_SECONDS"], "Use a number of seconds.") from exc
    if value < 0:
        raise ConfigError(["LINKEDIN_RETRY_AFTER_MAX_SECONDS"], "Use 0 or more.")
    return value
