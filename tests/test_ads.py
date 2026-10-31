"""Advertising API request shaping and the guarded campaign writes."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import (
    ACCOUNT_ID,
    CAMPAIGN_ID,
    GROUP_ID,
    OTHER_ACCOUNT_ID,
    decoded,
    make_settings,
    params,
    route,
)

from linkedin_marketing import LinkedInClient
from linkedin_marketing.errors import NotAllowedError, WriteRefusedError

CAMPAIGN_PATH = f"/adAccounts/{ACCOUNT_ID}/adCampaigns/{CAMPAIGN_ID}"


def campaign(status="ACTIVE", daily="30.00", account=ACCOUNT_ID):
    return {
        "id": CAMPAIGN_ID,
        "name": "Example campaign",
        "status": status,
        "account": f"urn:li:sponsoredAccount:{account}",
        "campaignGroup": f"urn:li:sponsoredCampaignGroup:{GROUP_ID}",
        "dailyBudget": {"amount": daily, "currencyCode": "GBP"} if daily else None,
        "runSchedule": {"start": 1767225600000},
        "targetingCriteria": {
            "include": {"and": [{"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:1"]}}]}
        },
    }


class TestReads:
    def test_accounts_search_filters_to_allow_list(self, client, api):
        call = route(
            api,
            "GET",
            "/adAccounts?",
            json={"elements": [{"id": ACCOUNT_ID, "currency": "GBP"}, {"id": OTHER_ACCOUNT_ID}]},
        )
        accounts = client.ads.accounts.list()
        assert [a.id for a in accounts] == [ACCOUNT_ID]
        sent = params(call.calls[0])
        assert sent["q"] == "search"
        assert sent["search"] == "(status:(values:List(ACTIVE,DRAFT)))"
        assert sent["pageSize"] == "100"

    def test_account_outside_allow_list_refused_before_request(self, client, api):
        call = route(api, "GET", f"/adAccounts/{OTHER_ACCOUNT_ID}")
        with pytest.raises(NotAllowedError) as caught:
            client.ads.accounts.get(OTHER_ACCOUNT_ID)
        assert caught.value.entity_id == OTHER_ACCOUNT_ID
        assert not call.called

    def test_account_users(self, client, api):
        call = route(
            api,
            "GET",
            "/adAccountUsers",
            json={
                "elements": [
                    {
                        "account": f"urn:li:sponsoredAccount:{ACCOUNT_ID}",
                        "user": "urn:li:person:abc",
                        "role": "VIEWER",
                    }
                ]
            },
        )
        users = client.ads.accounts.users(ACCOUNT_ID)
        assert users[0].role == "VIEWER"
        assert params(call.calls[0])["accounts"] == f"urn%3Ali%3AsponsoredAccount%3A{ACCOUNT_ID}"

    def test_campaign_groups_default_statuses_exclude_completed(self, client, api):
        call = route(
            api, "GET", f"/adAccounts/{ACCOUNT_ID}/adCampaignGroups", json={"elements": []}
        )
        client.ads.campaign_groups.list(ACCOUNT_ID)
        assert params(call.calls[0])["search"] == "(status:(values:List(ACTIVE,PAUSED,DRAFT)))"

    def test_campaign_search_with_group_and_cursor(self, client, api):
        call = api.get(
            url__startswith=f"https://api.linkedin.com/rest/adAccounts/{ACCOUNT_ID}/adCampaigns"
        ).mock(
            side_effect=[
                httpx.Response(
                    200, json={"elements": [campaign()], "metadata": {"nextPageToken": "t/2"}}
                ),
                httpx.Response(200, json={"elements": [campaign()]}),
            ]
        )
        campaigns = client.ads.campaigns.list(ACCOUNT_ID, campaign_group_id=GROUP_ID)
        assert len(campaigns) == 2
        first = params(call.calls[0])
        assert first["search"] == (
            "(status:(values:List(ACTIVE,PAUSED,DRAFT,COMPLETED)),"
            f"campaignGroup:(values:List(urn%3Ali%3AsponsoredCampaignGroup%3A{GROUP_ID})))"
        )
        assert params(call.calls[1])["pageToken"] == "t%2F2"
        model = campaigns[0]
        assert model.daily_budget.to_dict() == {"amount": "30.00", "currency": "GBP"}
        assert model.targeting["include"][0]["facet"] == "locations"
        assert "raw" not in model.to_dict()

    def test_creatives_finder(self, client, api):
        call = route(
            api,
            "GET",
            f"/adAccounts/{ACCOUNT_ID}/creatives",
            json={
                "elements": [
                    {
                        "id": "urn:li:sponsoredCreative:5",
                        "campaign": f"urn:li:sponsoredCampaign:{CAMPAIGN_ID}",
                        "intendedStatus": "ACTIVE",
                    }
                ]
            },
        )
        creatives = client.ads.creatives.list(ACCOUNT_ID, campaign_ids=[CAMPAIGN_ID])
        request = call.calls[0].request
        assert request.headers["X-RestLi-Method"] == "FINDER"
        sent = params(call.calls[0])
        assert sent["q"] == "criteria"
        assert sent["campaigns"] == f"List(urn%3Ali%3AsponsoredCampaign%3A{CAMPAIGN_ID})"
        assert sent["intendedStatuses"] == "List(ACTIVE,PAUSED,DRAFT)"
        assert creatives[0].id == "5"

    def test_creative_get_uses_encoded_urn_key(self, client, api):
        call = route(
            api,
            "GET",
            f"/adAccounts/{ACCOUNT_ID}/creatives/urn%3Ali%3AsponsoredCreative%3A5",
            json={"id": "urn:li:sponsoredCreative:5"},
        )
        assert client.ads.creatives.get(ACCOUNT_ID, 5).id == "5"
        assert call.called


class TestTargetingAndPlanning:
    def test_typeahead(self, client, api):
        call = route(
            api,
            "GET",
            "/adTargetingEntities",
            json={
                "elements": [
                    {
                        "urn": "urn:li:title:1",
                        "name": "Finance Director",
                        "facetUrn": "urn:li:adTargetingFacet:titles",
                    }
                ]
            },
        )
        entities = client.ads.targeting.typeahead("titles", "finance director")
        assert entities[0].name == "Finance Director"
        sent = params(call.calls[0])
        assert sent["q"] == "typeahead"
        assert sent["facet"] == "urn%3Ali%3AadTargetingFacet%3Atitles"
        assert sent["query"] == "finance%20director"
        assert sent["queryVersion"] == "QUERY_USES_URNS"
        assert sent["locale"] == "(language:en,country:US)"

    def test_facets_and_resolve(self, client, api):
        route(
            api,
            "GET",
            "/adTargetingFacets",
            json={
                "elements": [
                    {"facetName": "titles", "adTargetingFacetUrn": "urn:li:adTargetingFacet:titles"}
                ]
            },
        )
        assert client.ads.targeting.facets()[0]["facet"] == "titles"
        call = route(api, "GET", "/adTargetingEntities", json={"elements": []})
        client.ads.targeting.resolve(["urn:li:function:1", "urn:li:function:1"])
        assert params(call.calls[0])["urns"] == "List(urn%3Ali%3Afunction%3A1)"
        assert client.ads.targeting.resolve([]) == []

    def test_similar_and_facet_entities(self, client, api):
        call = route(api, "GET", "/adTargetingEntities", json={"elements": []})
        client.ads.targeting.similar("employers", ["urn:li:organization:1"])
        client.ads.targeting.entities("seniorities")
        assert params(call.calls[0])["q"] == "similarEntities"
        assert params(call.calls[1])["q"] == "adTargetingFacet"

    def test_audience_count(self, client, api):
        call = route(
            api, "GET", "/audienceCounts", json={"elements": [{"total": 52000, "active": 0}]}
        )
        criteria = {
            "include": {"and": [{"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:1"]}}]}
        }
        assert client.ads.targeting.audience_count(criteria) == {"total": 52000, "active": 0}
        assert params(call.calls[0])["q"] == "targetingCriteriaV2"

    def test_preview(self, client, api):
        call = route(
            api,
            "GET",
            "/adPreviews",
            json={
                "elements": [
                    {
                        "preview": "<iframe></iframe>",
                        "placement": {"linkedin": {"placementName": "FEED"}},
                    }
                ]
            },
        )
        previews = client.ads.previews.creative(ACCOUNT_ID, 5)
        assert previews[0]["preview_html"] == "<iframe></iframe>"
        sent = params(call.calls[0])
        assert sent["creative"] == "urn%3Ali%3AsponsoredCreative%3A5"
        assert sent["account"] == f"urn%3Ali%3AsponsoredAccount%3A{ACCOUNT_ID}"

    def test_budget_pricing(self, client, api):
        call = route(
            api,
            "GET",
            "/adBudgetPricing",
            json={
                "elements": [
                    {"suggestedBid": {"default": {"amount": "7.5", "currencyCode": "GBP"}}}
                ]
            },
        )
        result = client.ads.forecasting.budget_pricing(
            ACCOUNT_ID, {"include": {"and": []}}, "GBP", daily_budget="50"
        )
        assert result["suggested_bid"]["default"] == {"amount": "7.50", "currency": "GBP"}
        sent = decoded(call.calls[0])
        assert sent["q"] == "criteriaV2"
        assert sent["dailyBudget"] == "(amount:50,currencyCode:GBP)"
        assert sent["bidType"] == "CPM"

    def test_supply_forecast(self, client, api):
        from datetime import date

        call = route(
            api,
            "GET",
            "/adSupplyForecasts",
            json={
                "elements": [{"metricType": "IMPRESSION", "granularity": "DAILY", "timeSeries": []}]
            },
        )
        forecast = client.ads.forecasting.supply_forecast(
            ACCOUNT_ID, {}, date(2030, 1, 1), date(2030, 1, 8), "20", "GBP"
        )
        assert forecast[0]["metric"] == "IMPRESSION"
        assert params(call.calls[0])["timeRange"] == "(start:1893456000000,end:1894060800000)"


class TestCampaignWrites:
    def test_status_dry_run_sends_nothing(self, client, api, settings):
        route(api, "GET", CAMPAIGN_PATH, json=campaign())
        update = route(api, "POST", CAMPAIGN_PATH, status=204)
        result = client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "paused")
        assert result.dry_run is True
        assert result.before == "ACTIVE"
        assert result.after == "PAUSED"
        assert result.request["headers"]["X-RestLi-Method"] == "PARTIAL_UPDATE"
        assert result.request["json"] == {"patch": {"$set": {"status": "PAUSED"}}}
        assert not update.called
        assert not settings.audit_log_path.exists()

    def test_status_confirm_sends_and_audits(self, client, api, settings):
        route(api, "GET", CAMPAIGN_PATH, json=campaign())
        update = route(api, "POST", CAMPAIGN_PATH, status=204)
        result = client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "PAUSED", confirm=True)
        assert result.dry_run is False
        assert result.result == "applied"
        sent = update.calls[0].request
        assert sent.headers["X-RestLi-Method"] == "PARTIAL_UPDATE"
        assert json.loads(sent.content) == {"patch": {"$set": {"status": "PAUSED"}}}
        lines = [json.loads(line) for line in settings.audit_log_path.read_text().splitlines()]
        assert [line["event"] for line in lines] == [
            "linkedin.write.attempted",
            "linkedin.write.succeeded",
        ]
        assert lines[0]["campaign_id"] == CAMPAIGN_ID

    def test_read_only_mode_refuses_confirm(self, tmp_path, api):
        settings = make_settings(tmp_path, read_only=True)
        route(api, "GET", CAMPAIGN_PATH, json=campaign())
        update = route(api, "POST", CAMPAIGN_PATH, status=204)
        with LinkedInClient(settings, httpx.Client()) as client:
            dry = client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "PAUSED")
            assert "Read-only mode is on" in dry.result
            with pytest.raises(WriteRefusedError) as caught:
                client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "PAUSED", confirm=True)
        assert caught.value.reason == "read_only_mode"
        assert not update.called

    def test_noop_is_not_sent(self, client, api):
        route(api, "GET", CAMPAIGN_PATH, json=campaign(status="PAUSED"))
        update = route(api, "POST", CAMPAIGN_PATH, status=204)
        result = client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "PAUSED", confirm=True)
        assert result.dry_run is True
        assert result.result.startswith("no_change")
        assert not update.called

    @pytest.mark.parametrize("current", ["DRAFT", "ARCHIVED", "COMPLETED"])
    def test_cannot_launch_or_revive(self, client, api, current):
        route(api, "GET", CAMPAIGN_PATH, json=campaign(status=current))
        with pytest.raises(WriteRefusedError) as caught:
            client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "ACTIVE")
        assert caught.value.reason == "current_status_not_switchable"

    def test_status_must_be_active_or_paused(self, client, api):
        with pytest.raises(WriteRefusedError) as caught:
            client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "ARCHIVED")
        assert caught.value.reason == "invalid_input"

    def test_campaign_from_other_account_refused(self, client, api):
        route(api, "GET", CAMPAIGN_PATH, json=campaign(account=OTHER_ACCOUNT_ID))
        with pytest.raises(WriteRefusedError) as caught:
            client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "PAUSED")
        assert caught.value.reason == "not_in_account"

    def test_failed_write_is_audited_and_raised(self, client, api, settings):
        route(api, "GET", CAMPAIGN_PATH, json=campaign())
        route(api, "POST", CAMPAIGN_PATH, status=422, json={"message": "bad"})
        with pytest.raises(Exception):  # noqa: B017
            client.ads.campaigns.set_status(ACCOUNT_ID, CAMPAIGN_ID, "PAUSED", confirm=True)
        events = [json.loads(x)["event"] for x in settings.audit_log_path.read_text().splitlines()]
        assert events == ["linkedin.write.attempted", "linkedin.write.failed"]

    def test_daily_budget_dry_run_keeps_currency_and_2dp(self, client, api):
        route(api, "GET", CAMPAIGN_PATH, json=campaign(daily="30"))
        result = client.ads.campaigns.set_daily_budget(ACCOUNT_ID, CAMPAIGN_ID, "45.5")
        assert result.before == {"amount": "30.00", "currency": "GBP"}
        assert result.after == {"amount": "45.50", "currency": "GBP"}
        assert result.request["json"]["patch"]["$set"]["dailyBudget"] == {
            "amount": "45.50",
            "currencyCode": "GBP",
        }

    @pytest.mark.parametrize(
        ("amount", "reason"),
        [
            ("0", "budget_not_positive"),
            ("abc", "budget_not_positive"),
            ("Infinity", "budget_not_positive"),
            ("100.01", "budget_over_cap"),
        ],
    )
    def test_daily_budget_guards(self, client, api, amount, reason):
        route(api, "GET", CAMPAIGN_PATH, json=campaign())
        with pytest.raises(WriteRefusedError) as caught:
            client.ads.campaigns.set_daily_budget(ACCOUNT_ID, CAMPAIGN_ID, amount)
        assert caught.value.reason == reason

    def test_daily_budget_refused_without_cap(self, tmp_path, api):
        settings = make_settings(tmp_path, max_daily_budget=None)
        with (
            LinkedInClient(settings, httpx.Client()) as client,
            pytest.raises(WriteRefusedError) as caught,
        ):
            client.ads.campaigns.set_daily_budget(ACCOUNT_ID, CAMPAIGN_ID, "10")
        assert caught.value.reason == "no_budget_cap"

    def test_daily_budget_refused_without_existing_daily_budget(self, client, api):
        route(api, "GET", CAMPAIGN_PATH, json=campaign(daily=None))
        with pytest.raises(WriteRefusedError) as caught:
            client.ads.campaigns.set_daily_budget(ACCOUNT_ID, CAMPAIGN_ID, "10")
        assert caught.value.reason == "no_daily_budget"

    def test_campaign_group_status(self, client, api):
        path = f"/adAccounts/{ACCOUNT_ID}/adCampaignGroups/{GROUP_ID}"
        route(api, "GET", path, json={"id": GROUP_ID, "name": "Group", "status": "PAUSED"})
        result = client.ads.campaign_groups.set_status(ACCOUNT_ID, GROUP_ID, "ACTIVE")
        assert result.dry_run is True
        assert result.request["url"].endswith(path)
        assert result.after == "ACTIVE"
