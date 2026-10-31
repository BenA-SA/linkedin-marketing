"""One place that sends requests: headers, a 401 retry, error mapping, paging.

Resources build a ``Request`` (method, path, pre-encoded query pairs, body,
extra headers) and hand it here. The same ``Request`` doubles as the exact,
token-free description a dry run returns, so what a dry run shows is what
``send`` would put on the wire.

LinkedIn pages in two styles: a cursor (``pageSize`` + ``pageToken``, next
token in ``metadata.nextPageToken``) on the ads search finders, and an
offset (``start`` + ``count``) almost everywhere else. Both iterators stop
at ``Settings.max_pages`` so no call can loop unboundedly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx

from linkedin_marketing import restli
from linkedin_marketing.auth import MINT_HINT, TokenProvider
from linkedin_marketing.config import API_ROOT, Settings
from linkedin_marketing.errors import ApiError, AuthError, RateLimitError

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2.0.0"
_FORBIDDEN_HINT = (
    "LinkedIn answered 403: the token lacks a scope for this product, the app "
    "is not approved for it, or the member has no role on the account or page."
)
_UNAUTHORISED_HINT = (
    f"LinkedIn refused the access token (HTTP 401) even after a refresh. {MINT_HINT}"
)


@dataclass(frozen=True)
class Request:
    """A fully shaped API call, minus the ``Authorization`` header."""

    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    json: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def with_query(self, *extra: tuple[str, str]) -> Request:
        return Request(self.method, self.path, (*self.query, *extra), self.json, self.headers)

    @property
    def url(self) -> str:
        url = f"{API_ROOT}{self.path}"
        return f"{url}?{restli.build_query(self.query)}" if self.query else url

    def describe(self, api_version: str) -> dict:
        """The request as a dict a human can review (no token)."""
        headers = {
            "LinkedIn-Version": api_version,
            "X-Restli-Protocol-Version": PROTOCOL_VERSION,
            **self.headers,
        }
        if self.json is not None:
            headers["Content-Type"] = "application/json"
        return {"method": self.method, "url": self.url, "headers": headers, "json": self.json}


@dataclass(frozen=True)
class Response:
    """Status, parsed JSON body (``{}`` when empty) and the created entity id."""

    status_code: int
    body: dict
    restli_id: str = ""


class Transport:
    """Sends ``Request`` objects with auth, versioning and error mapping."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client,
        tokens: TokenProvider,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self._http = http
        self._tokens = tokens
        self._sleep = sleep

    def send(self, request: Request) -> Response:
        """Send one call and map errors.

        Retries once with a fresh token on 401, and once after a short
        ``Retry-After`` on 429 (at most ``retry_after_max_seconds``). Longer
        waits, typically a used-up daily quota, raise ``RateLimitError`` with
        ``retry_after`` so the caller decides.
        """
        response = self._send_once(request)
        if response.status_code == 401 and self._tokens.can_refresh:
            self._tokens.invalidate()
            response = self._send_once(request)
        wait = _short_retry_after(response, self.settings.retry_after_max_seconds)
        if wait is not None:
            logger.info("linkedin.rate_limited retry_in=%s path=%s", wait, request.path)
            self._sleep(wait)
            response = self._send_once(request)
        raise_for_status(request.path, response)
        body = response.json() if response.content else {}
        return Response(
            response.status_code,
            body if isinstance(body, dict) else {"value": body},
            response.headers.get("x-restli-id", ""),
        )

    def get(self, path: str, query=(), headers: dict | None = None) -> dict:
        return self.send(Request("GET", path, tuple(query), None, headers or {})).body

    def iter_cursor(self, request: Request, page_size: int) -> Iterator[dict]:
        """Elements across ``pageToken`` pages, up to ``max_pages`` pages."""
        page_token = ""
        for _ in range(self.settings.max_pages):
            page = request.with_query(("pageSize", str(page_size)))
            if page_token:
                page = page.with_query(("pageToken", restli.encode(page_token)))
            body = self.send(page).body
            yield from body.get("elements", [])
            page_token = (body.get("metadata") or {}).get("nextPageToken") or ""
            if not page_token:
                return

    def iter_offset(self, request: Request, count: int, start: int = 0) -> Iterator[dict]:
        """Elements across ``start``/``count`` pages, up to ``max_pages`` pages."""
        for _ in range(self.settings.max_pages):
            page = request.with_query(("start", str(start)), ("count", str(count)))
            body = self.send(page).body
            elements = body.get("elements", [])
            yield from elements
            start += len(elements)
            if not _has_more(body.get("paging") or {}, start, len(elements), count):
                return

    def _headers(self, request: Request) -> dict:
        headers = {
            "Authorization": f"Bearer {self._tokens.access_token()}",
            "LinkedIn-Version": self.settings.api_version,
            "X-Restli-Protocol-Version": PROTOCOL_VERSION,
            **request.headers,
        }
        return headers

    def _send_once(self, request: Request) -> httpx.Response:
        try:
            return self._http.request(
                request.method,
                request.url,
                json=request.json,
                headers=self._headers(request),
            )
        except httpx.HTTPError as exc:
            raise ApiError(request.path, 0, type(exc).__name__) from exc


def _short_retry_after(response: httpx.Response, max_seconds: float) -> float | None:
    """Seconds to wait before one retry of a 429, or None to raise instead."""
    if response.status_code != 429:
        return None
    raw = response.headers.get("Retry-After", "").strip()
    if not raw.isdigit():
        return None
    seconds = float(raw)
    return seconds if seconds <= max_seconds else None


def _has_more(paging: dict, next_start: int, returned: int, count: int) -> bool:
    """Whether another offset page exists, from ``total`` or a full page."""
    if not returned:
        return False
    total = paging.get("total")
    if isinstance(total, int):
        return next_start < total
    links = paging.get("links") or []
    if any(link.get("rel") == "next" for link in links if isinstance(link, dict)):
        return True
    return returned >= count


def raise_for_status(endpoint: str, response: httpx.Response) -> None:
    """Map a non-2xx response to the matching ``LinkedInError`` subclass."""
    status = response.status_code
    if status < 400:
        return
    body = response.text
    if status == 401:
        raise AuthError(_UNAUTHORISED_HINT, 401, body, endpoint)
    if status == 403:
        raise AuthError(_FORBIDDEN_HINT, 403, body, endpoint)
    if status == 429:
        raise RateLimitError(endpoint, body, response.headers.get("Retry-After", ""))
    raise ApiError(endpoint, status, body)
