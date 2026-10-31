"""Shared fixtures: fake credentials, a mocked LinkedIn, and a wired client.

All ids are placeholders; no request ever reaches LinkedIn.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest
import respx

from linkedin_marketing import LinkedInClient, Settings
from linkedin_marketing.config import API_ROOT, TOKEN_URL

ACCOUNT_ID = 123456789
OTHER_ACCOUNT_ID = 987654321
ORG_ID = 11111111
CAMPAIGN_ID = 222222222
GROUP_ID = 333333333
ACCESS_TOKEN = "fake-access-token"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "client_id": "fake-client-id",
        "client_secret": "fake-client-secret",
        "refresh_token": "fake-refresh-token",
        "modules": ("all",),
        "read_only": False,
        "allowed_ad_account_ids": (ACCOUNT_ID,),
        "allowed_organization_ids": (ORG_ID,),
        "max_daily_budget": Decimal("100"),
        "audit_log_path": tmp_path / "writes.jsonl",
        "retry_after_max_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def api():
    """A respx router with the token endpoint pre-mocked."""
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "expires_in": 5184000,
                    "refresh_token_expires_in": 31536000,
                },
            )
        )
        yield router


@pytest.fixture
def client(settings, api):
    with LinkedInClient(settings, httpx.Client()) as wired:
        yield wired


def route(api, method: str, path: str, **response):
    """Mock ``{API_ROOT}{path}`` (any query) with a JSON response."""
    response.setdefault("json", {})
    return api.route(method=method, url__startswith=f"{API_ROOT}{path}").mock(
        return_value=httpx.Response(response.pop("status", 200), **response)
    )


def raw_query(call) -> str:
    """The query string exactly as sent (still percent-encoded)."""
    return call.request.url.query.decode()


def params(call) -> dict[str, str]:
    """Query parameters split on ``&``/``=`` with values left encoded."""
    pairs = (part.split("=", 1) for part in raw_query(call).split("&") if part)
    return dict(pairs)


def decoded(call) -> dict[str, str]:
    return {key: unquote(value) for key, value in params(call).items()}
