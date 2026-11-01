"""adAnalytics shaping, derived metrics, scoping, and Ad Library search."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from conftest import ACCOUNT_ID, CAMPAIGN_ID, make_settings, params, route

from linkedin_marketing import LinkedInClient
from linkedin_marketing.config import API_ROOT
from linkedin_marketing.errors import RateLimitError
from linkedin_marketing.resources.reporting import derived_metrics

START = date(2026, 10, 1)
END = date(2026, 10, 31)


def element(pivot, impressions=1000, clicks=20, cost="40.123", leads=4, opens=10, day=None):
    start = day or START
    end = day or END
    return {
        "pivotValues": [pivot],
        "dateRange": {
            "start": {"year": start.year, "month": start.month, "day": start.day},
            "end": {"year": end.year, "month": end.month, "day": end.day},
        },
        "impressions": impressions,
        "clicks": clicks,
        "costInLocalCurrency": cost,
        "oneClickLeads": leads,
        "oneClickLeadFormOpens": opens,
    }


@pytest.fixture
def account(api):
    return api.get(f"{API_ROOT}/adAccounts/{ACCOUNT_ID}").mock(
        return_value=httpx.Response(200, json={"id": ACCOUNT_ID, "currency": "GBP"})
    )


class TestAnalytics:
    def test_query_shape_and_derived_metrics(self, client, api, account):
        call = route(
            api,
            "GET",
            "/adAnalytics",
            json={"elements": [element(f"urn:li:sponsoredCampaign:{CAMPAIGN_ID}")]},
        )
        report = client.reporting.analytics(ACCOUNT_ID, START, END)
        sent = params(call.calls[0])
        assert sent["q"] == "analytics"
        assert sent["pivot"] == "CAMPAIGN"
        assert sent["timeGranularity"] == "ALL"
        assert (
            sent["dateRange"]
            == "(start:(year:2026,month:10,day:1),end:(year:2026,month:10,day:31))"
        )
        assert sent["accounts"] == f"List(urn%3Ali%3AsponsoredAccount%3A{ACCOUNT_ID})"
        assert sent["fields"].startswith("dateRange,pivotValues,impressions,clicks")
        row = report.to_records()[0]
        assert row["pivot_id"] == str(CAMPAIGN_ID)
        assert row["currency"] == "GBP"
        assert row["costInLocalCurrency"] == "40.12"
        assert row["ctr_percent"] == "2.00"
        assert row["cpc"] == "2.01"
        assert row["cpm"] == "40.12"
        assert row["cpl"] == "10.03"
        assert row["lead_form_completion_percent"] == "40.00"

    def test_totals_sum_rows_and_money_is_2dp(self, client, api, account):
        route(
            api,
            "GET",
            "/adAnalytics",
            json={
                "elements": [
                    element("urn:li:sponsoredCampaign:1", cost="10.005"),
                    element("urn:li:sponsoredCampaign:2", cost="0.10", clicks=0, leads=0),
                ]
            },
        )
        totals = client.reporting.analytics(ACCOUNT_ID, START, END).totals()
        assert totals["impressions"] == 2000
        assert totals["costInLocalCurrency"] == "10.11"
        assert Decimal(totals["cpl"]).as_tuple().exponent == -2

    def test_zero_denominators_are_none(self):
        assert derived_metrics({"impressions": 0, "clicks": 0})["ctr_percent"] is None
        assert derived_metrics({"costInLocalCurrency": Decimal("5")})["cpl"] is None

    def test_campaign_facet_after_ownership_check(self, client, api, account):
        search = route(
            api,
            "GET",
            f"/adAccounts/{ACCOUNT_ID}/adCampaigns",
            json={"elements": [{"id": CAMPAIGN_ID, "status": "ACTIVE"}]},
        )
        call = route(api, "GET", "/adAnalytics", json={"elements": []})
        client.reporting.analytics(ACCOUNT_ID, START, campaign_ids=[CAMPAIGN_ID])
        assert f"urn%3Ali%3AsponsoredCampaign%3A{CAMPAIGN_ID}" in params(search.calls[0])["search"]
        sent = params(call.calls[0])
        assert sent["campaigns"] == f"List(urn%3Ali%3AsponsoredCampaign%3A{CAMPAIGN_ID})"
        assert "accounts" not in sent
        assert sent["dateRange"] == "(start:(year:2026,month:10,day:1))"

    def test_campaign_outside_account_refused(self, client, api, account):
        route(api, "GET", f"/adAccounts/{ACCOUNT_ID}/adCampaigns", json={"elements": []})
        analytics = route(api, "GET", "/adAnalytics")
        with pytest.raises(ValueError, match="not in ad account"):
            client.reporting.analytics(ACCOUNT_ID, START, campaign_ids=[CAMPAIGN_ID])
        assert not analytics.called

    def test_statistics_pivots(self, client, api, account):
        call = route(api, "GET", "/adAnalytics", json={"elements": []})
        client.reporting.statistics(
            ACCOUNT_ID, START, ["campaign", "creative"], time_granularity="daily"
        )
        sent = params(call.calls[0])
        assert sent["q"] == "statistics"
        assert sent["pivots"] == "List(CAMPAIGN,CREATIVE)"
        assert sent["timeGranularity"] == "DAILY"
        with pytest.raises(ValueError):
            client.reporting.statistics(ACCOUNT_ID, START, ["A", "B", "C", "D"])

    def test_too_many_metrics(self, client):
        with pytest.raises(ValueError, match="at most 20"):
            client.reporting.analytics(ACCOUNT_ID, START, metrics=[f"m{i}" for i in range(21)])

    def test_demographics_resolves_names_and_drops_withheld_metrics(self, client, api, account):
        call = route(api, "GET", "/adAnalytics", json={"elements": [element("urn:li:function:12")]})
        route(
            api,
            "GET",
            "/adTargetingEntities",
            json={"elements": [{"urn": "urn:li:function:12", "name": "Finance"}]},
        )
        report = client.reporting.demographics(
            ACCOUNT_ID, START, metrics=["impressions", "approximateMemberReach"]
        )
        assert "approximateMemberReach" not in params(call.calls[0])["fields"]
        record = report.to_dict()
        assert record["rows"][0]["pivot_names"] == ["Finance"]
        assert "approximate" in record["notes"][0]

    def test_demographics_keeps_data_when_names_fail(self, client, api, account):
        route(api, "GET", "/adAnalytics", json={"elements": [element("urn:li:function:12")]})
        route(api, "GET", "/adTargetingEntities", status=500)
        report = client.reporting.demographics(ACCOUNT_ID, START, "member_seniority")
        assert report.rows[0].pivot_values == ["urn:li:function:12"]
        assert any("names unavailable" in note for note in report.notes)

    def test_demographics_needs_member_pivot(self, client):
        with pytest.raises(ValueError):
            client.reporting.demographics(ACCOUNT_ID, START, "CAMPAIGN")


class TestAdLibrary:
    def test_query_shape(self, client, api):
        call = route(
            api,
            "GET",
            "/adLibrary",
            json={
                "elements": [
                    {
                        "adUrl": "https://www.linkedin.com/ad-library/detail/1",
                        "isRestricted": False,
                        "details": {
                            "type": "SPONSORED_STATUS_UPDATE",
                            "advertiser": {
                                "advertiserName": "Example Widgets Ltd",
                                "adPayer": "Example Widgets Ltd",
                                "advertiserUrl": "https://www.linkedin.com/company/example",
                            },
                            "adTargeting": [{"facetName": "Location"}],
                        },
                    }
                ],
                "paging": {"total": 1},
            },
        )
        page = client.ad_library.search(
            keyword="data analytics",
            countries=["GB", "gb", "IE"],
            start_date=date(2026, 9, 1),
            end_date=date(2026, 10, 1),
        )
        sent = params(call.calls[0])
        assert sent["q"] == "criteria"
        assert sent["keyword"] == "data%20analytics"
        assert sent["countries"] == "List(urn%3Ali%3Acountry%3Agb,urn%3Ali%3Acountry%3Aie)"
        assert (
            sent["dateRange"] == "(start:(year:2026,month:9,day:1),end:(year:2026,month:10,day:1))"
        )
        assert sent["count"] == "25"
        ad = page.ads[0]
        assert ad.advertiser_name == "Example Widgets Ltd"
        assert ad.ad_type == "SPONSORED_STATUS_UPDATE"
        assert page.next_start is None

    def test_pages_at_most_25_and_stops_at_limit(self, client, api):
        call = api.get(url__startswith="https://api.linkedin.com/rest/adLibrary").mock(
            side_effect=[
                httpx.Response(
                    200, json={"elements": [{"adUrl": "a"}] * 25, "paging": {"total": 100}}
                ),
                httpx.Response(
                    200, json={"elements": [{"adUrl": "b"}] * 15, "paging": {"total": 100}}
                ),
            ]
        )
        page = client.ad_library.search(advertiser="Example", limit=40)
        assert len(page.ads) == 40
        assert params(call.calls[1])["start"] == "25"
        assert params(call.calls[1])["count"] == "15"
        assert page.next_start == 40

    def test_page_cap(self, tmp_path, api):
        settings = make_settings(tmp_path, max_pages=1)
        call = route(api, "GET", "/adLibrary", json={"elements": [{"adUrl": "a"}] * 25})
        with LinkedInClient(settings, httpx.Client()) as client:
            page = client.ad_library.search(payer="Example", limit=100)
        assert call.call_count == 1
        assert params(call.calls[0])["payerName"] == "Example"
        assert page.next_start == 25

    def test_needs_a_filter_and_valid_countries(self, client):
        with pytest.raises(ValueError):
            client.ad_library.search()
        with pytest.raises(ValueError):
            client.ad_library.search(keyword="x", countries=["GBR"])

    def test_429_after_first_page_returns_ads_already_read(self, tmp_path, api):
        first_page = {
            "elements": [
                {"adUrl": f"https://www.linkedin.com/ad-library/detail/{n}"} for n in range(25)
            ],
            "paging": {"start": 0, "count": 25, "total": 100},
        }
        api.get(url__startswith=f"{API_ROOT}/adLibrary").mock(
            side_effect=[
                httpx.Response(200, json=first_page),
                httpx.Response(429, json={"message": "Too many", "period": "ONE_MINUTE"}),
            ]
        )
        with LinkedInClient(make_settings(tmp_path), httpx.Client()) as client:
            page = client.ad_library.search(keyword="x", limit=75)
        assert len(page.ads) == 25
        assert page.next_start == 25
        result = page.to_dict()
        assert result["stopped"] == "rate_limited"
        assert result["rate_limit"]["period"] == "ONE_MINUTE"

    def test_quota_exhausted_is_typed(self, client, api):
        route(api, "GET", "/adLibrary", status=429, json={"serviceErrorCode": 101})
        with pytest.raises(RateLimitError) as caught:
            client.ad_library.search(keyword="x")
        assert caught.value.service_error_code == 101
