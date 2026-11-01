"""Rest.li encoding, settings, auth, transport errors, retries and paging."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from conftest import ACCESS_TOKEN, ACCOUNT_ID, make_settings, route

from linkedin_marketing import LinkedInClient, restli
from linkedin_marketing.config import TOKEN_URL, Settings
from linkedin_marketing.errors import (
    ApiError,
    AuthError,
    ConfigError,
    ModuleDisabledError,
    RateLimitError,
)
from linkedin_marketing.modules import scopes_for
from linkedin_marketing.transport import Request


class TestRestli:
    def test_urn_list_encodes_urns_but_not_structure(self):
        value = restli.urn_list(["urn:li:sponsoredAccount:1", "urn:li:sponsoredAccount:2"])
        assert value == "List(urn%3Ali%3AsponsoredAccount%3A1,urn%3Ali%3AsponsoredAccount%3A2)"

    def test_date_range_with_and_without_end(self):
        assert (
            restli.date_range(date(2026, 1, 2), date(2026, 3, 4))
            == "(start:(year:2026,month:1,day:2),end:(year:2026,month:3,day:4))"
        )
        assert restli.date_range(date(2026, 1, 2)) == "(start:(year:2026,month:1,day:2))"

    def test_search_criteria_drops_empty(self):
        assert (
            restli.search_criteria(status=["ACTIVE", "PAUSED"], id=[])
            == "(status:(values:List(ACTIVE,PAUSED)))"
        )

    def test_serialize_targeting_criteria(self):
        criteria = {
            "include": {"and": [{"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:1"]}}]}
        }
        assert restli.serialize(criteria) == (
            "(include:(and:List((or:(urn%3Ali%3AadTargetingFacet%3Alocations:"
            "List(urn%3Ali%3Ageo%3A1))))))"
        )

    def test_serialize_encodes_parens_inside_urns(self):
        assert restli.serialize("urn:li:x:(a,b)") == "urn%3Ali%3Ax%3A%28a%2Cb%29"


class TestSettings:
    def test_defaults_are_safe(self):
        settings = Settings.from_env({})
        assert settings.read_only is True
        assert settings.modules == ("ads", "reporting", "ad_library")
        assert settings.allowed_ad_account_ids == ()
        assert settings.max_daily_budget is None
        assert settings.api_version == "202610"

    def test_parses_everything(self, tmp_path):
        settings = Settings.from_env(
            {
                "LINKEDIN_MODULES": "ads, leads",
                "LINKEDIN_READ_ONLY": "false",
                "LINKEDIN_ALLOWED_AD_ACCOUNT_IDS": "1, 2",
                "LINKEDIN_ALLOWED_ORGANIZATION_IDS": "3",
                "LINKEDIN_MAX_DAILY_BUDGET": "50.5",
                "LINKEDIN_AUDIT_LOG": str(tmp_path / "a.jsonl"),
                "LINKEDIN_MAX_PAGES": "3",
                "LINKEDIN_RETRY_AFTER_MAX_SECONDS": "0",
            }
        )
        assert settings.modules == ("ads", "leads")
        assert settings.read_only is False
        assert settings.allowed_ad_account_ids == (1, 2)
        assert settings.allowed_organization_ids == (3,)
        assert settings.max_daily_budget == Decimal("50.5")
        assert settings.max_pages == 3
        assert settings.retry_after_max_seconds == 0

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("LINKEDIN_MODULES", "ads,nope"),
            ("LINKEDIN_READ_ONLY", "maybe"),
            ("LINKEDIN_ALLOWED_AD_ACCOUNT_IDS", "abc"),
            ("LINKEDIN_MAX_DAILY_BUDGET", "-1"),
            ("LINKEDIN_MAX_PAGES", "0"),
        ],
    )
    def test_rejects_bad_values_naming_only_the_variable(self, name, value):
        with pytest.raises(ConfigError) as caught:
            Settings.from_env({name: value})
        assert caught.value.names == [name]

    def test_all_modules(self):
        assert len(Settings(modules=("all",)).modules) == 8

    def test_secrets_not_in_repr(self):
        settings = Settings(client_secret="s3cret", refresh_token="r3fresh", access_token="a3ccess")
        assert "s3cret" not in repr(settings)
        assert "r3fresh" not in repr(settings)
        assert "a3ccess" not in repr(settings)

    def test_scopes_for_modules(self):
        assert scopes_for(("ads", "reporting")) == ["r_ads", "rw_ads", "r_ads_reporting"]
        assert scopes_for(("ads",), include_writes=False) == ["r_ads"]


class TestAuthAndTransport:
    def test_refresh_then_bearer_and_version_headers(self, client, api):
        call = route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", json={"id": ACCOUNT_ID})
        client.ads.accounts.get(ACCOUNT_ID)
        headers = call.calls[0].request.headers
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert headers["LinkedIn-Version"] == "202610"
        assert headers["X-Restli-Protocol-Version"] == "2.0.0"
        token_body = api.calls[0].request.content.decode()
        assert "grant_type=refresh_token" in token_body

    def test_token_cached_across_calls(self, client, api):
        route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", json={"id": ACCOUNT_ID})
        client.ads.accounts.get(ACCOUNT_ID)
        client.ads.accounts.get(ACCOUNT_ID)
        assert sum(1 for call in api.calls if str(call.request.url) == TOKEN_URL) == 1

    def test_static_access_token(self, tmp_path, api):
        settings = make_settings(tmp_path, refresh_token="", access_token="static-token")
        call = route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", json={"id": ACCOUNT_ID})
        with LinkedInClient(settings, httpx.Client()) as client:
            client.ads.accounts.get(ACCOUNT_ID)
        assert call.calls[0].request.headers["Authorization"] == "Bearer static-token"

    def test_missing_credentials(self, tmp_path, api):
        settings = make_settings(tmp_path, refresh_token="", client_id="", client_secret="")
        with LinkedInClient(settings, httpx.Client()) as client, pytest.raises(ConfigError):
            client.ads.accounts.get(ACCOUNT_ID)

    def test_401_retries_once_with_fresh_token(self, client, api):
        call = api.get(url__startswith="https://api.linkedin.com/rest/adAccounts/").mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json={"id": ACCOUNT_ID})]
        )
        client.ads.accounts.get(ACCOUNT_ID)
        assert call.call_count == 2

    def test_refresh_rejected(self, client, api):
        api.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        with pytest.raises(AuthError) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        assert caught.value.endpoint == "oauth/accessToken"

    @pytest.mark.parametrize(
        ("status", "error"), [(403, AuthError), (404, ApiError), (500, ApiError)]
    )
    def test_error_mapping(self, client, api, status, error):
        route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", status=status, json={"message": "x"})
        with pytest.raises(error) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        assert caught.value.context["status_code"] == status

    def test_429_long_retry_after_raises_typed_error(self, client, api):
        route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", status=429, headers={"Retry-After": "3600"})
        with pytest.raises(RateLimitError) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        assert caught.value.retry_after == "3600"

    def test_429_keeps_what_linkedin_said_about_the_limit(self, client, api):
        route(
            api,
            "GET",
            f"/adAccounts/{ACCOUNT_ID}",
            status=429,
            json={"serviceErrorCode": 101, "message": "Throttled", "period": "ONE_MINUTE"},
            headers={"X-RateLimit-Remaining": "0", "x-li-uuid": "abc==", "Set-Cookie": "x=y"},
        )
        with pytest.raises(RateLimitError) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        error = caught.value
        assert error.period == "ONE_MINUTE"
        assert error.api_message == "Throttled"
        assert error.service_error_code == 101
        assert error.headers == {"x-ratelimit-remaining": "0", "x-li-uuid": "abc=="}
        assert error.context["period"] == "ONE_MINUTE"
        assert "Limit period: ONE_MINUTE" in str(error)
        assert "Daily quotas reset" not in str(error)

    @pytest.mark.parametrize(
        ("message", "period"),
        [("Quota exceeded for period DAY", "DAY"), ("Too many requests per minute", "per minute")],
    )
    def test_429_bare_period_words_are_recognised(self, client, api, message, period):
        route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", status=429, json={"message": message})
        with pytest.raises(RateLimitError) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        assert caught.value.period == period
        assert "No limit period was recognised" not in str(caught.value)

    def test_429_without_details_does_not_assume_daily(self, client, api):
        route(api, "GET", f"/adAccounts/{ACCOUNT_ID}", status=429, json={"status": 429})
        with pytest.raises(RateLimitError) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        assert caught.value.period == ""
        assert "No limit period was recognised" in str(caught.value)
        assert 'Body: {"status":429}' in str(caught.value)

    def test_429_short_retry_after_waits_and_retries(self, tmp_path, api):
        settings = make_settings(tmp_path, retry_after_max_seconds=5)
        call = api.get(url__startswith="https://api.linkedin.com/rest/adAccounts/").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json={"id": ACCOUNT_ID}),
            ]
        )
        waits = []
        with LinkedInClient(settings, httpx.Client()) as client:
            client.transport._sleep = waits.append
            client.ads.accounts.get(ACCOUNT_ID)
        assert waits == [2.0]
        assert call.call_count == 2

    def test_network_error_becomes_api_error(self, client, api):
        api.get(url__startswith="https://api.linkedin.com/rest/adAccounts/").mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(ApiError) as caught:
            client.ads.accounts.get(ACCOUNT_ID)
        assert caught.value.status_code == 0

    def test_disabled_module(self, tmp_path, api):
        settings = make_settings(tmp_path, modules=("ads",))
        with LinkedInClient(settings, httpx.Client()) as client:
            with pytest.raises(ModuleDisabledError):
                _ = client.leads
            assert client.ads is not None

    def test_offset_paging_stops_at_max_pages(self, tmp_path, api):
        settings = make_settings(tmp_path, max_pages=2)
        call = route(
            api,
            "GET",
            "/adAccountUsers",
            json={"elements": [{"role": "VIEWER"}] * 2, "paging": {"total": 100}},
        )
        with LinkedInClient(settings, httpx.Client()) as client:
            users = client.ads.accounts.users(ACCOUNT_ID, limit=None)
        assert call.call_count == 2
        assert len(users) == 4

    def test_request_describe_has_no_token(self):
        described = Request("POST", "/x", json={"a": 1}).describe("202610")
        assert "Authorization" not in described["headers"]
        assert described["headers"]["Content-Type"] == "application/json"
        assert json.dumps(described)
