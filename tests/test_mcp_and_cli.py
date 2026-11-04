"""The MCP layer stays thin: right tools per module, calls reach the library.
Also the mint-token CLI helpers."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import ACCOUNT_ID, CAMPAIGN_ID, make_settings, route

from linkedin_marketing import LinkedInClient
from linkedin_marketing.config import TOKEN_URL
from linkedin_marketing.mcp.server import build_server
from linkedin_marketing.mint_token import (
    MintError,
    _parse_args,
    _scopes,
    authorisation_url,
    code_from_query,
    exchange_code,
)


def tool_names(server) -> set[str]:
    return {tool.name for tool in asyncio.run(server.list_tools())}


def read_only_hint(tool) -> bool | None:
    """``readOnlyHint`` (mcp 1.x) or ``read_only_hint`` (mcp 2.x)."""
    annotations = tool.annotations
    return getattr(annotations, "read_only_hint", getattr(annotations, "readOnlyHint", None))


def call(server, name: str, arguments: dict):
    """Call a tool and return its JSON payload, across mcp SDK 1.x and 2.x."""
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
    return json.loads(content[0].text)


class TestServer:
    def test_only_enabled_modules_get_tools(self, tmp_path):
        server = build_server(make_settings(tmp_path, modules=("ad_library",)), lambda: None)
        assert tool_names(server) == {"li_search_ad_library"}

    def test_all_modules(self, tmp_path):
        names = tool_names(build_server(make_settings(tmp_path), lambda: None))
        assert {
            "li_ad_analytics",
            "li_set_campaign_status",
            "li_list_leads",
            "li_send_conversion_events",
            "li_update_audience_contacts",
            "li_create_organization_post",
            "li_list_events",
        } <= names
        assert not any("delete" in name for name in names)

    def test_write_tools_are_annotated_and_described(self, tmp_path):
        server = build_server(make_settings(tmp_path), lambda: None)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        write = tools["li_set_campaign_status"]
        assert read_only_hint(write) is False
        assert "DRY RUN" in write.description
        assert read_only_hint(tools["li_list_campaigns"]) is True
        assert tools["li_list_leads"].description.startswith("[EXPERIMENTAL")

    def test_write_tool_defaults_to_dry_run_through_library(self, tmp_path, api):
        settings = make_settings(tmp_path)
        path = f"/adAccounts/{ACCOUNT_ID}/adCampaigns/{CAMPAIGN_ID}"
        route(
            api,
            "GET",
            path,
            json={
                "id": CAMPAIGN_ID,
                "status": "ACTIVE",
                "name": "C",
                "account": f"urn:li:sponsoredAccount:{ACCOUNT_ID}",
            },
        )
        update = route(api, "POST", path, status=204)
        client = LinkedInClient(settings, httpx.Client())
        server = build_server(settings, lambda: client)
        payload = call(
            server, "li_set_campaign_status", {"campaign_id": CAMPAIGN_ID, "status": "PAUSED"}
        )
        assert payload["dry_run"] is True
        assert not update.called

    def test_read_tool_uses_default_account(self, tmp_path, api):
        settings = make_settings(tmp_path)
        call_route = route(
            api,
            "GET",
            f"/adAccounts/{ACCOUNT_ID}/adCampaignGroups",
            json={"elements": [{"id": 1, "name": "G", "status": "ACTIVE"}]},
        )
        client = LinkedInClient(settings, httpx.Client())
        server = build_server(settings, lambda: client)
        payload = call(server, "li_list_campaign_groups", {})
        assert call_route.called
        assert "G" in json.dumps(payload)

    def test_conversion_tools_registered_and_annotated(self, tmp_path):
        server = build_server(make_settings(tmp_path, modules=("conversions",)), lambda: None)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        assert set(tools) == {
            "li_list_conversion_rules",
            "li_create_conversion_rule",
            "li_create_conversion_rules",
            "li_list_campaign_conversions",
            "li_associate_campaigns_to_conversion",
            "li_send_conversion_events",
        }
        for name in (
            "li_create_conversion_rule",
            "li_create_conversion_rules",
            "li_associate_campaigns_to_conversion",
            "li_send_conversion_events",
        ):
            assert read_only_hint(tools[name]) is False
            assert "DRY RUN" in tools[name].description
        assert read_only_hint(tools["li_list_campaign_conversions"]) is True

    def test_create_rule_tool_passes_auto_association(self, tmp_path, api):
        settings = make_settings(tmp_path)
        post = route(api, "POST", "/conversions")
        client = LinkedInClient(settings, httpx.Client())
        server = build_server(settings, lambda: client)
        payload = call(
            server,
            "li_create_conversion_rule",
            {"name": "Lead", "rule_type": "LEAD", "auto_association": "OBJECTIVE_BASED"},
        )
        assert payload["dry_run"] is True
        assert payload["request"]["url"].endswith("?autoAssociationType=OBJECTIVE_BASED")
        assert payload["request"]["json"]["attributionType"] == "LAST_TOUCH_BY_CAMPAIGN"
        assert not post.called

    def test_associate_tool_dry_runs_through_library(self, tmp_path, api):
        settings = make_settings(tmp_path)
        route(api, "GET", "/conversions/44", json={"id": 44, "name": "R", "enabled": True})
        route(
            api,
            "GET",
            f"/adAccounts/{ACCOUNT_ID}/adCampaigns",
            json={"elements": [{"id": CAMPAIGN_ID, "status": "ACTIVE"}]},
        )
        route(api, "GET", "/campaignConversions?", json={"elements": []})
        put = route(api, "PUT", "/campaignConversions", status=204)
        client = LinkedInClient(settings, httpx.Client())
        server = build_server(settings, lambda: client)
        payload = call(
            server,
            "li_associate_campaigns_to_conversion",
            {"rule_id": 44, "campaign_ids": [CAMPAIGN_ID]},
        )
        assert payload["dry_run"] is True
        assert payload["request"]["method"] == "PUT"
        assert not put.called

    def test_send_events_tool_accepts_lead(self, tmp_path, api):
        settings = make_settings(tmp_path)
        route(api, "GET", "/conversions/44", json={"id": 44, "name": "R", "enabled": True})
        client = LinkedInClient(settings, httpx.Client())
        server = build_server(settings, lambda: client)
        lead = "urn:li:leadGenFormResponse:00000000-0000-0000-0000-000000000000-4"
        happened = datetime.now(UTC).isoformat()
        payload = call(
            server,
            "li_send_conversion_events",
            {"rule_id": 44, "events": [{"happened_at": happened, "lead": lead}]},
        )
        assert payload["request"]["json"]["elements"][0]["user"] == {
            "userIds": [],
            "lead": lead,
        }


class TestMintToken:
    def test_authorisation_url(self):
        url = authorisation_url("cid", "http://localhost:8765/callback", "st", ["r_ads", "rw_ads"])
        assert "scope=r_ads+rw_ads" in url
        assert "state=st" in url

    def test_scope_presets(self):
        assert _scopes(_parse_args(["--modules", "ads,reporting"])) == [
            "r_ads",
            "rw_ads",
            "r_ads_reporting",
        ]
        assert _scopes(_parse_args(["--modules", "leads", "--read-only-scopes"])) == [
            "r_marketing_leadgen_automation"
        ]
        assert _scopes(_parse_args(["--scopes", "a, b"])) == ["a", "b"]

    def test_code_from_query(self):
        assert code_from_query("code=abc&state=st", "st") == "abc"
        with pytest.raises(MintError, match="state"):
            code_from_query("code=abc&state=other", "st")
        with pytest.raises(MintError, match="refused"):
            code_from_query("error=user_cancelled_login&state=st", "st")

    def test_exchange_requires_refresh_token(self, api):
        api.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "x"}))
        with httpx.Client() as http, pytest.raises(MintError, match="no refresh token"):
            exchange_code(http, "cid", "secret", "code", "http://localhost:8765/callback")
