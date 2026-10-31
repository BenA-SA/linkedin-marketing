"""Every failure mode of the library, one class each, under ``LinkedInError``.

Each error carries the values that caused it in ``context`` (and as named
attributes) so a caller can log them as structured fields or branch on them
instead of parsing the message. ``context`` never holds a token or the client
secret: response bodies are truncated and LinkedIn does not echo credentials.
"""

from __future__ import annotations

_BODY_MAX_CHARS = 500


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
    """LinkedIn answered 429: an application or member quota is used up."""

    def __init__(self, endpoint: str, body: str = "", retry_after: str = ""):
        super().__init__(
            f"LinkedIn rate limit hit on {endpoint} (HTTP 429). Daily quotas reset "
            "at midnight UTC; see the app's Analytics tab in the Developer Portal.",
            status_code=429,
            body=_truncate(body),
            endpoint=endpoint,
            retry_after=retry_after,
        )
        self.status_code = 429
        self.endpoint = endpoint
        self.retry_after = retry_after


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
