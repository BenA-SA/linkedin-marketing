"""Access tokens: a static one, or one minted from a refresh token and cached.

LinkedIn access tokens last about 60 days and programmatic refresh tokens
about 365. Refreshing does not extend the refresh token, so only the refresh
token needs storing and each process mints its own access token on first use.
Tokens never leave this module except as the ``Authorization`` header and
are never logged.

Docs: https://learn.microsoft.com/linkedin/shared/authentication/programmatic-refresh-tokens
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from linkedin_marketing.config import TOKEN_URL, Settings
from linkedin_marketing.errors import ApiError, AuthError, ConfigError

logger = logging.getLogger(__name__)

_EXPIRY_SAFETY_SECONDS = 300
_REFRESH_REJECTED_STATUSES = frozenset({400, 401})
_REFRESH_WARNING_DAYS = 30
MINT_HINT = "Mint a new one with `linkedin-marketing-token` (see the README)."


class TokenProvider:
    """Hands out a valid access token, refreshing it when missing or stale."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._http = http
        self._clock = clock
        self._access_token = ""
        self._expires_at = 0.0

    @property
    def can_refresh(self) -> bool:
        return self._settings.uses_refresh_token

    def access_token(self) -> str:
        """The cached or static token, or a freshly minted one."""
        if not self.can_refresh:
            return self._static_token()
        if self._access_token and self._clock() < self._expires_at:
            return self._access_token
        self._refresh()
        return self._access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next call refreshes (after an API 401)."""
        self._access_token = ""
        self._expires_at = 0.0

    def _static_token(self) -> str:
        if not self._settings.access_token:
            raise ConfigError(self._settings.missing_credentials(), MINT_HINT)
        return self._settings.access_token

    def _refresh(self) -> None:
        missing = self._settings.missing_credentials()
        if missing:
            raise ConfigError(missing, MINT_HINT)
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._settings.refresh_token,
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise ApiError("oauth/accessToken", 0, type(exc).__name__) from exc
        self._store(response)

    def _store(self, response: httpx.Response) -> None:
        if response.status_code in _REFRESH_REJECTED_STATUSES:
            raise AuthError(
                "LinkedIn refused the refresh token (expired, revoked, or minted "
                f"for other scopes). {MINT_HINT}",
                status_code=response.status_code,
                body=error_summary(response),
                endpoint="oauth/accessToken",
            )
        if response.status_code >= 400:
            raise ApiError("oauth/accessToken", response.status_code, error_summary(response))
        payload = response.json()
        expires_in = int(payload.get("expires_in", 0))
        self._access_token = payload["access_token"]
        self._expires_at = self._clock() + max(expires_in - _EXPIRY_SAFETY_SECONDS, 0)
        _log_refresh_token_age(payload)


def error_summary(response: httpx.Response) -> str:
    """``error: error_description`` from the token endpoint, never the request."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if not isinstance(payload, dict):
        return str(payload)[:200]
    return f"{payload.get('error', '')}: {payload.get('error_description', '')}"


def _log_refresh_token_age(payload: dict) -> None:
    days_left = int(payload.get("refresh_token_expires_in", 0)) // 86400
    if not days_left:
        return
    level = logging.WARNING if days_left < _REFRESH_WARNING_DAYS else logging.INFO
    logger.log(level, "linkedin.refresh_token days_left=%s", days_left)
