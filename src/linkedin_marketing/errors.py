"""Every failure mode of the library, one class each, under ``LinkedInError``.

Each error carries the values that caused it in ``context`` (and as named
attributes) so a caller can log them as structured fields or branch on them
instead of parsing the message. ``context`` never holds a token or the client
secret: response bodies are truncated and LinkedIn does not echo credentials.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

_BODY_MAX_CHARS = 500
_PERIOD_FIELD = re.compile(
    r'"?(?:period|quotaPeriod|timeWindow|window)"?\s*[:=]\s*"?([A-Za-z0-9_]+)'
)
_PERIOD_TOKEN = re.compile(
    r"\b(?:(?:ONE|FIVE|TEN|FIFTEEN|THIRTY|SIXTY)_)?(?:SECOND|MINUTE|HOUR|DAY)S?\b"
    r"|\bDAILY\b|\bper (?:second|minute|hour|day)\b|\bdaily\b"
)
_LIMIT_HEADER_HINTS = ("rate", "retry", "quota", "limit", "throttl")
_REQUEST_ID_HEADER = "x-li-uuid"


def _truncate(body: str) -> str:
    return body[:_BODY_MAX_CHARS]


class LinkedInError(Exception):
    """Base for every failure raised by this library, carrying structured context."""

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.context = context


class ConfigError(LinkedInError):
    """A setting is missing or malformed. Carries variable names, never values."""

    def __init__(self, names: list[str], hint: str = ""):
        super().__init__(
            f"linkedin-marketing is not configured: set {', '.join(names)}. {hint}".strip(),
            names=names,
        )
        self.names = names


class ModuleDisabledError(LinkedInError):
    """A resource from a module that is not in ``LINKEDIN_MODULES`` was used."""

    def __init__(self, module: str, enabled: tuple[str, ...]):
        super().__init__(
            f"The '{module}' module is disabled. Enabled: {', '.join(enabled) or 'none'}. "
            f"Add it to LINKEDIN_MODULES (and make sure your LinkedIn app is approved "
            f"for the matching product).",
            module=module,
            enabled=list(enabled),
        )
        self.module = module


class AuthError(LinkedInError):
    """The token is missing, expired or revoked, or lacks a scope or role.

    ``status_code`` is 0 when no request was made. 401 from the API means the
    access token was refused; 400/401 from the token endpoint means the
    refresh token was; 403 means a missing scope, product or role.
    """

    def __init__(self, message: str, status_code: int = 0, body: str = "", endpoint: str = ""):
        super().__init__(message, status_code=status_code, body=_truncate(body), endpoint=endpoint)
        self.status_code = status_code
        self.body = _truncate(body)
        self.endpoint = endpoint


class RateLimitError(LinkedInError):
    """LinkedIn answered 429: a per-minute throttle or a daily quota was hit.

    LinkedIn does not publish its limits and a 200 response carries no quota
    headers, so the 429 itself is the only place it says *which* limit fired.
    Everything it sent is kept: ``period`` (parsed from the body or headers
    when present, else ``""``), ``api_message``, ``service_error_code``, the
    limit-related ``headers`` plus the ``x-li-uuid`` request id, and the
    truncated ``body``. The message is built from those values and never
    assumes the limit was daily.
    """

    def __init__(
        self,
        endpoint: str,
        body: str = "",
        retry_after: str = "",
        headers: Mapping[str, str] | None = None,
    ):
        limit_headers = _limit_headers(headers or {})
        api_message, service_error_code = _parse_error_body(body)
        period = _find_period(body, limit_headers)
        self.status_code = 429
        self.endpoint = endpoint
        self.retry_after = retry_after
        self.period = period
        self.api_message = api_message
        self.service_error_code = service_error_code
        self.headers = limit_headers
        self.body = _truncate(body)
        super().__init__(
            _rate_limit_message(endpoint, self.details()),
            status_code=429,
            endpoint=endpoint,
            **self.details(),
        )

    def details(self) -> dict:
        """What LinkedIn said about the limit, as plain JSON-ready values."""
        return {
            "period": self.period,
            "retry_after": self.retry_after,
            "api_message": self.api_message,
            "service_error_code": self.service_error_code,
            "headers": self.headers,
            "body": self.body,
        }


def _limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers that describe a rate limit, plus LinkedIn's request id for support."""
    kept = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered == _REQUEST_ID_HEADER or any(hint in lowered for hint in _LIMIT_HEADER_HINTS):
            kept[lowered] = value
    return kept


def _parse_error_body(body: str) -> tuple[str, int | None]:
    """LinkedIn's ``message`` and ``serviceErrorCode`` from a JSON error body."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return "", None
    if not isinstance(parsed, dict):
        return "", None
    code = parsed.get("serviceErrorCode")
    return str(parsed.get("message") or ""), code if isinstance(code, int) else None


def _find_period(body: str, headers: Mapping[str, str]) -> str:
    """The limit's period (e.g. ``ONE_MINUTE``) if LinkedIn named one anywhere."""
    for text in (body, *headers.values()):
        field = _PERIOD_FIELD.search(text)
        if field:
            return field.group(1)
        token = _PERIOD_TOKEN.search(text)
        if token:
            return token.group(0)
    return ""


def _rate_limit_message(endpoint: str, details: dict) -> str:
    parts = [f"LinkedIn rate limit hit on {endpoint} (HTTP 429)."]
    if details["api_message"]:
        parts.append(f"LinkedIn said: {details['api_message']!r}.")
    if details["period"]:
        parts.append(f"Limit period: {details['period']}.")
    if details["retry_after"]:
        parts.append(f"Retry-After: {details['retry_after']}s.")
    if not (details["period"] or details["retry_after"]):
        parts.append(
            "No limit period was recognised in the response (check the body below): "
            "a per-minute throttle clears within a minute, a daily quota resets at "
            "00:00 UTC."
        )
    parts.append("Exact quotas: the app's Analytics tab in the Developer Portal.")
    if details["headers"]:
        parts.append(f"Headers: {json.dumps(details['headers'], sort_keys=True)}.")
    if details["body"]:
        parts.append(f"Body: {details['body']}")
    return " ".join(parts)


class ApiError(LinkedInError):
    """Any other non-2xx answer, or LinkedIn unreachable (``status_code`` 0)."""

    def __init__(self, endpoint: str, status_code: int, body: str = ""):
        super().__init__(
            f"LinkedIn API {endpoint} failed with HTTP {status_code}: {_truncate(body)}",
            status_code=status_code,
            body=_truncate(body),
            endpoint=endpoint,
        )
        self.status_code = status_code
        self.body = _truncate(body)
        self.endpoint = endpoint


class NotAllowedError(LinkedInError):
    """A call named an ad account or organization that is not on its allow-list."""

    def __init__(self, kind: str, entity_id: int, allowed: tuple[int, ...], env: str):
        super().__init__(
            f"{kind} {entity_id} is not allow-listed ({env}={','.join(map(str, allowed))}).",
            kind=kind,
            entity_id=entity_id,
            allowed=list(allowed),
        )
        self.kind = kind
        self.entity_id = entity_id


class WriteRefusedError(LinkedInError):
    """A guarded write was refused before any request was sent.

    ``reason`` is a closed vocabulary, for example ``read_only_mode``,
    ``status_not_allowed``, ``current_status_not_switchable``,
    ``not_in_account``, ``budget_not_positive``, ``budget_over_cap``,
    ``no_budget_cap``, ``no_daily_budget``, ``batch_too_large``,
    ``invalid_input``.
    """

    def __init__(self, reason: str, action: str, detail: str, **context):
        super().__init__(f"Refused {action}: {detail}", reason=reason, action=action, **context)
        self.reason = reason
        self.action = action
