"""Request shaping and validation for the Conversions API's three steps.

Rules (create, batch create, ownership listing), campaign associations
(``/campaignConversions``) and event identifiers. Built from the docs; these
tests pin the exact requests so a live-API correction is a one-line diff.
All ids, names and addresses are placeholders.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ACCOUNT_ID, CAMPAIGN_ID, OTHER_ACCOUNT_ID, make_settings, params, route

from linkedin_marketing import (
    CampaignNotInAccountError,
    ConversionEvent,
    ConversionRuleSpec,
    LinkedInClient,
    hash_name,
)
from linkedin_marketing.errors import NotAllowedError, WriteRefusedError
from linkedin_marketing.resources.conversions import association_key, batch_failures

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
ACCOUNT_URN = f"urn:li:sponsoredAccount:{ACCOUNT_ID}"
SECOND_CAMPAIGN_ID = 222222223
RULE_ID = 44
RULE_URN = f"urn:lla:llaPartnerConversion:{RULE_ID}"
LEAD_URN = "urn:li:leadGenFormResponse:00000000-0000-0000-0000-000000000000-4"
CAMPAIGN_KEY = (
    f"(campaign:urn%3Ali%3AsponsoredCampaign%3A{CAMPAIGN_ID},"
    f"conversion:urn%3Alla%3AllaPartnerConversion%3A{RULE_ID})"
)


def raw_path(call) -> str:
    """The request path exactly as sent (percent-escapes intact)."""
    return call.request.url.raw_path.decode().split("?", 1)[0]


def mock_rule(api, enabled=True):
    return route(
        api,
        "GET",
        f"/conversions/{RULE_ID}",
        json={"id": RULE_ID, "name": "CRM lead", "type": "LEAD", "enabled": enabled},
    )


def mock_campaigns(api, *ids):
    return route(
        api,
        "GET",
        f"/adAccounts/{ACCOUNT_ID}/adCampaigns",
        json={"elements": [{"id": i, "status": "ACTIVE", "account": ACCOUNT_URN} for i in ids]},
    )


def mock_associations(api, *campaign_ids):
    elements = [
        {"associatedAt": 1, "campaign": f"urn:li:sponsoredCampaign:{i}", "conversion": RULE_URN}
        for i in campaign_ids
    ]
    return route(api, "GET", "/campaignConversions?", json={"elements": elements})


class TestRules:
    def test_list_owned_only_by_default(self, client, api):
        call = route(api, "GET", "/conversions?", json={"elements": []})
        client.conversions.rules(ACCOUNT_ID)
        assert "conversionOwnershipTypes" not in params(call.calls[0])

    def test_list_including_shared(self, client, api):
        call = route(
            api,
            "GET",
            "/conversions?",
            json={
                "elements": [
                    {"id": 7, "name": "Shared", "ownershipType": "SHARED", "account": ACCOUNT_URN}
                ]
            },
        )
        rules = client.conversions.rules(ACCOUNT_ID, include_shared=True)
        sent = params(call.calls[0])
        assert sent["q"] == "account"
        assert sent["conversionOwnershipTypes"] == "List(OWNED,SHARED)"
        assert rules[0].ownership_type == "SHARED"
        assert rules[0].account_id == str(ACCOUNT_ID)

    def test_create_rule_with_auto_association(self, client, api):
        post = route(api, "POST", "/conversions", status=201, headers={"x-restli-id": "9"})
        result = client.conversions.create_rule(
            ACCOUNT_ID,
            "Lead form lead",
            "lead",
            90,
            30,
            True,
            attribution_type="last_touch_by_conversion",
            auto_association="all_campaigns",
        )
        assert result.created_id == "9"
        request = post.calls[0].request
        assert request.url.query.decode() == "autoAssociationType=ALL_CAMPAIGNS"
        assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
        assert json.loads(request.content) == {
            "name": "Lead form lead",
            "account": ACCOUNT_URN,
            "conversionMethod": "CONVERSIONS_API",
            "type": "LEAD",
            "postClickAttributionWindowSize": 90,
            "viewThroughAttributionWindowSize": 30,
            "attributionType": "LAST_TOUCH_BY_CONVERSION",
        }
        assert not result.warning

    def test_create_rule_without_association_warns(self, client):
        result = client.conversions.create_rule(ACCOUNT_ID, "Lead", auto_association=None)
        assert result.dry_run
        assert result.request["url"].endswith("/conversions")
        assert "No campaigns will be associated" in result.warning
        assert result.summary["auto_association"] == "NONE"

    def test_create_rule_objective_based(self, client):
        result = client.conversions.create_rule(
            ACCOUNT_ID, "Lead", auto_association="OBJECTIVE_BASED"
        )
        assert result.request["url"].endswith("/conversions?autoAssociationType=OBJECTIVE_BASED")

    @pytest.mark.parametrize(
        ("rule_type", "post_click", "view_through", "field"),
        [
            ("LEAD", 14, 7, "post_click_window_days"),
            ("LEAD", 30, 28, "view_through_window_days"),
            ("SIGN_UP", 180, 7, "post_click_window_days"),
            ("KEY_PAGE_VIEW", 30, 365, "view_through_window_days"),
            ("NOT_A_TYPE", 30, 7, "rule_type"),
        ],
    )
    def test_invalid_windows_and_types_refused(
        self, client, rule_type, post_click, view_through, field
    ):
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.create_rule(ACCOUNT_ID, "R", rule_type, post_click, view_through)
        assert caught.value.reason == "invalid_input"
        assert caught.value.context["field"] == field

    @pytest.mark.parametrize(
        ("rule_type", "post_click", "view_through"),
        [
            ("LEAD", 28, 1),
            ("QUALIFIED_LEAD", 365, 180),
            ("PURCHASE", 180, 365),
            ("SALES_QUALIFIED_LEAD", 365, 90),
        ],
    )
    def test_valid_windows_accepted(self, client, rule_type, post_click, view_through):
        result = client.conversions.create_rule(
            ACCOUNT_ID, "R", rule_type, post_click, view_through
        )
        assert result.request["json"]["postClickAttributionWindowSize"] == post_click
        assert result.request["json"]["viewThroughAttributionWindowSize"] == view_through

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"attribution_type": "FIRST_TOUCH"}, "attribution_type"),
            ({"auto_association": "SOME_CAMPAIGNS"}, "auto_association"),
        ],
    )
    def test_invalid_attribution_and_association_refused(self, client, kwargs, field):
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.create_rule(ACCOUNT_ID, "R", **kwargs)
        assert caught.value.context["field"] == field

    def test_blank_name_refused(self, client):
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.create_rule(ACCOUNT_ID, "   ")
        assert caught.value.context["field"] == "name"

    def test_qualified_lead_types_need_202608(self, tmp_path, api):
        settings = make_settings(tmp_path, api_version="202607")
        with LinkedInClient(settings) as old:
            with pytest.raises(WriteRefusedError) as caught:
                old.conversions.create_rule(ACCOUNT_ID, "MQL", "MARKETING_QUALIFIED_LEAD")
            assert caught.value.reason == "version_too_old"
            assert old.conversions.create_rule(ACCOUNT_ID, "QL", "QUALIFIED_LEAD").dry_run

    def test_create_rule_outside_allow_list(self, client):
        with pytest.raises(NotAllowedError):
            client.conversions.create_rule(OTHER_ACCOUNT_ID, "R")


class TestBatchRules:
    def test_batch_create_shape(self, client, api):
        post = route(
            api,
            "POST",
            "/conversions",
            json={"elements": [{"status": 201, "id": "1"}, {"status": 201, "id": "2"}]},
        )
        specs = [
            ConversionRuleSpec("Lead", "LEAD"),
            ConversionRuleSpec("Qualified", "QUALIFIED_LEAD", 90, 30),
        ]
        result = client.conversions.create_rules(
            ACCOUNT_ID, specs, confirm=True, auto_association="OBJECTIVE_BASED"
        )
        request = post.calls[0].request
        assert request.headers["X-RestLi-Method"] == "BATCH_CREATE"
        assert request.url.query.decode() == "autoAssociationType=OBJECTIVE_BASED"
        elements = json.loads(request.content)["elements"]
        assert [element["type"] for element in elements] == ["LEAD", "QUALIFIED_LEAD"]
        assert all(element["account"] == ACCOUNT_URN for element in elements)
        assert result.result == "applied"
        assert result.summary["rule_count"] == 2

    def test_batch_partial_failure_is_reported(self, client, api):
        route(
            api,
            "POST",
            "/conversions",
            json={"elements": [{"status": 201, "id": "1"}, {"status": 400}]},
        )
        result = client.conversions.create_rules(
            ACCOUNT_ID, [ConversionRuleSpec("A"), ConversionRuleSpec("B")], confirm=True
        )
        assert result.result == "partially_applied"
        assert "1 failed element(s): 1" in result.warning

    @pytest.mark.parametrize("count", [0, 101])
    def test_batch_size_limits(self, client, count):
        specs = [ConversionRuleSpec(f"Rule {i}") for i in range(count)]
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.create_rules(ACCOUNT_ID, specs)
        assert caught.value.reason == "batch_too_large"

    def test_one_invalid_spec_refuses_the_batch(self, client):
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.create_rules(
                ACCOUNT_ID, [ConversionRuleSpec("A"), ConversionRuleSpec("B", "SIGN_UP", 365)]
            )
        assert caught.value.reason == "invalid_input"


class TestCampaignConversions:
    def test_list_associations(self, client, api):
        search = mock_campaigns(api, CAMPAIGN_ID, SECOND_CAMPAIGN_ID)
        call = route(
            api,
            "GET",
            "/campaignConversions?",
            json={
                "elements": [
                    {
                        "associatedAt": 1503516717694,
                        "campaign": f"urn:li:sponsoredCampaign:{CAMPAIGN_ID}",
                        "conversion": RULE_URN,
                    },
                    {
                        "associatedAt": 1563657869695,
                        "campaign": f"urn:li:sponsoredCampaign:{SECOND_CAMPAIGN_ID}",
                        "conversion": "urn:lla:llaPartnerConversion:99",
                    },
                ],
                "paging": {"count": 100, "start": 0, "links": []},
            },
        )
        rows = client.conversions.campaign_conversions(
            ACCOUNT_ID, [CAMPAIGN_ID, SECOND_CAMPAIGN_ID]
        )
        assert f"urn%3Ali%3AsponsoredCampaign%3A{CAMPAIGN_ID}" in params(search.calls[0])["search"]
        sent = params(call.calls[0])
        assert sent["q"] == "campaigns"
        assert sent["campaigns"] == (
            f"List(urn%3Ali%3AsponsoredCampaign%3A{CAMPAIGN_ID},"
            f"urn%3Ali%3AsponsoredCampaign%3A{SECOND_CAMPAIGN_ID})"
        )
        assert [(row.campaign_id, row.rule_id) for row in rows] == [
            (CAMPAIGN_ID, RULE_ID),
            (SECOND_CAMPAIGN_ID, 99),
        ]
        only_rule = client.conversions.campaign_conversions(
            ACCOUNT_ID, [CAMPAIGN_ID, SECOND_CAMPAIGN_ID], rule_id=RULE_ID
        )
        assert [row.campaign_id for row in only_rule] == [CAMPAIGN_ID]

    def test_list_refuses_campaigns_outside_account(self, client, api):
        mock_campaigns(api)
        lookup = route(api, "GET", "/campaignConversions?")
        with pytest.raises(CampaignNotInAccountError) as caught:
            client.conversions.campaign_conversions(ACCOUNT_ID, [CAMPAIGN_ID])
        assert caught.value.campaign_ids == [CAMPAIGN_ID]
        assert not lookup.called

    def test_list_needs_a_campaign(self, client):
        with pytest.raises(ValueError, match="at least one campaign"):
            client.conversions.campaign_conversions(ACCOUNT_ID, [])

    def test_associate_one_campaign_is_a_put_on_its_key(self, client, api):
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID)
        mock_associations(api)
        put = route(api, "PUT", "/campaignConversions/", status=204)
        result = client.conversions.associate_campaigns(
            ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID], confirm=True
        )
        request = put.calls[0].request
        assert raw_path(put.calls[0]) == f"/rest/campaignConversions/{CAMPAIGN_KEY}"
        assert not request.url.query
        assert "X-RestLi-Method" not in request.headers
        assert json.loads(request.content) == {
            "campaign": f"urn:li:sponsoredCampaign:{CAMPAIGN_ID}",
            "conversion": RULE_URN,
        }
        assert result.result == "applied"
        assert result.status_code == 204
        assert (result.before, result.after) == ([], [CAMPAIGN_ID])

    def test_associate_several_is_one_batch_update(self, client, api):
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID, SECOND_CAMPAIGN_ID)
        mock_associations(api)
        second_key = CAMPAIGN_KEY.replace(str(CAMPAIGN_ID), str(SECOND_CAMPAIGN_ID))
        put = route(
            api,
            "PUT",
            "/campaignConversions?",
            json={"results": {CAMPAIGN_KEY: {"status": 204}, second_key: {"status": 204}}},
        )
        result = client.conversions.associate_campaigns(
            ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID, SECOND_CAMPAIGN_ID, CAMPAIGN_ID], confirm=True
        )
        request = put.calls[0].request
        assert request.headers["X-RestLi-Method"] == "BATCH_UPDATE"
        assert request.url.query.decode() == f"ids=List({CAMPAIGN_KEY},{second_key})"
        entities = json.loads(request.content)["entities"]
        assert list(entities) == [CAMPAIGN_KEY, second_key]
        assert entities[second_key]["campaign"] == f"urn:li:sponsoredCampaign:{SECOND_CAMPAIGN_ID}"
        assert result.result == "applied"
        assert result.summary == {"campaign_count": 2}

    def test_associate_dry_run_sends_only_missing(self, client, api):
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID, SECOND_CAMPAIGN_ID)
        mock_associations(api, CAMPAIGN_ID)
        put = route(api, "PUT", "/campaignConversions")
        result = client.conversions.associate_campaigns(
            ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID, SECOND_CAMPAIGN_ID]
        )
        assert result.dry_run
        assert not put.called
        assert result.request["method"] == "PUT"
        assert str(SECOND_CAMPAIGN_ID) in result.request["url"]
        assert f"sponsoredCampaign%3A{CAMPAIGN_ID}," not in result.request["url"]
        assert (result.before, result.after) == ([CAMPAIGN_ID], [CAMPAIGN_ID, SECOND_CAMPAIGN_ID])

    def test_associate_already_associated_is_a_noop(self, client, api):
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID)
        mock_associations(api, CAMPAIGN_ID)
        put = route(api, "PUT", "/campaignConversions")
        result = client.conversions.associate_campaigns(
            ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID], confirm=True
        )
        assert result.result.startswith("no_change")
        assert not put.called

    def test_associate_refuses_campaign_outside_account(self, client, api):
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID)
        put = route(api, "PUT", "/campaignConversions")
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.associate_campaigns(
                ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID, SECOND_CAMPAIGN_ID], confirm=True
            )
        assert caught.value.reason == "not_in_account"
        assert caught.value.context["campaign_ids"] == [SECOND_CAMPAIGN_ID]
        assert not put.called

    def test_associate_refuses_account_outside_allow_list(self, client, api):
        with pytest.raises(NotAllowedError):
            client.conversions.associate_campaigns(OTHER_ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID])

    @pytest.mark.parametrize("count", [0, 51])
    def test_associate_batch_limits(self, client, count):
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.associate_campaigns(ACCOUNT_ID, RULE_ID, range(1, count + 1))
        assert caught.value.reason == "batch_too_large"

    def test_associate_read_only_mode_refuses(self, tmp_path, api):
        settings = make_settings(tmp_path, read_only=True)
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID)
        mock_associations(api)
        guarded = LinkedInClient(settings)
        with guarded, pytest.raises(WriteRefusedError) as caught:
            guarded.conversions.associate_campaigns(
                ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID], confirm=True
            )
        assert caught.value.reason == "read_only_mode"

    def test_disabled_rule_warns(self, client, api):
        mock_rule(api, enabled=False)
        mock_campaigns(api, CAMPAIGN_ID)
        mock_associations(api)
        result = client.conversions.associate_campaigns(ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID])
        assert "disabled" in result.warning

    def test_batch_update_errors_are_reported(self, client, api):
        mock_rule(api)
        mock_campaigns(api, CAMPAIGN_ID, SECOND_CAMPAIGN_ID)
        mock_associations(api)
        route(
            api,
            "PUT",
            "/campaignConversions?",
            json={"results": {CAMPAIGN_KEY: {"status": 204}}, "errors": {"other-key": {}}},
        )
        result = client.conversions.associate_campaigns(
            ACCOUNT_ID, RULE_ID, [CAMPAIGN_ID, SECOND_CAMPAIGN_ID], confirm=True
        )
        assert result.result == "partially_applied"
        assert "other-key" in result.warning

    def test_association_key_and_failures_helpers(self):
        assert association_key(CAMPAIGN_ID, RULE_ID) == CAMPAIGN_KEY
        assert batch_failures({}) == []
        assert batch_failures({"results": {"k": {"status": 404}}, "errors": {"e": {}}}) == [
            "e",
            "k",
        ]


class TestEventIdentifiers:
    def to_api(self, api_version="202610", **fields):
        return ConversionEvent(happened_at=NOW, **fields).to_api(RULE_ID, NOW, api_version)

    def test_lead_only_sends_empty_user_ids(self):
        user = self.to_api(lead=f" {LEAD_URN} ")["user"]
        assert user == {"userIds": [], "lead": LEAD_URN}

    def test_external_id_only(self):
        assert self.to_api(external_id="crm-0001")["user"] == {
            "userIds": [],
            "externalIds": ["crm-0001"],
        }

    def test_plain_ip_address(self):
        user = self.to_api(ip_address=" 192.0.2.10 ")["user"]
        assert user["userIds"] == [{"idType": "PLAINTEXT_IP_ADDRESS", "idValue": "192.0.2.10"}]

    def test_names_only_send_user_info_with_empty_user_ids(self):
        user = self.to_api(first_name="Sam", last_name="Example", company="Example Ltd")["user"]
        assert user == {
            "userIds": [],
            "userInfo": {"firstName": "Sam", "lastName": "Example", "companyName": "Example Ltd"},
        }

    def test_hashed_names(self):
        info = self.to_api(first_name="Sam", last_name="O'Example", hash_names=True)["user"][
            "userInfo"
        ]
        assert info == {
            "hashedFirstName": hash_name("sam"),
            "hashedLastName": hash_name("oexample"),
        }
        assert "Sam" not in json.dumps(info)

    def test_mixed_plain_and_prehashed_names(self):
        digest = hash_name("Example")
        info = self.to_api(first_name="Sam", last_name=digest.upper())["user"]["userInfo"]
        assert info == {"firstName": "Sam", "hashedLastName": digest}

    def test_hashed_names_need_202609(self):
        with pytest.raises(ValueError, match="202609"):
            self.to_api("202608", first_name="Sam", last_name="Example", hash_names=True)

    def test_every_identifier_together(self):
        user = self.to_api(
            email="someone@example.com",
            li_fat_id="click-id",
            ip_address="198.51.100.7",
            lead=LEAD_URN,
            external_id="crm-0001",
            first_name="Sam",
            last_name="Example",
        )["user"]
        assert [item["idType"] for item in user["userIds"]] == [
            "SHA256_EMAIL",
            "LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID",
            "PLAINTEXT_IP_ADDRESS",
        ]
        assert set(user) == {"userIds", "userInfo", "lead", "externalIds"}

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({}, "needs an email"),
            ({"first_name": "Sam"}, "needs an email"),
            ({"external_id": "  "}, "needs an email"),
            ({"lead": "urn:li:sponsoredCampaign:1"}, "leadGenFormResponse"),
            ({"lead": "urn:li:leadGenFormResponse:"}, "leadGenFormResponse"),
            ({"ip_address": "2001:db8::1"}, "IPv4"),
            ({"ip_address": "not-an-ip"}, "IPv4"),
        ],
    )
    def test_invalid_identifiers(self, fields, message):
        with pytest.raises(ValueError, match=message):
            self.to_api(**fields)

    def test_name_hashing_normalises(self):
        assert hash_name(" Anne-Marie ") == hash_name("annemarie")
        with pytest.raises(ValueError):
            hash_name("--")


class TestSendEventsWithLead:
    def test_lead_event_through_send_events(self, client, api, settings):
        mock_rule(api)
        post = route(api, "POST", "/conversionEvents", status=201)
        event = ConversionEvent(happened_at=NOW - timedelta(hours=1), lead=LEAD_URN, event_id="e1")
        result = client.conversions.send_events(ACCOUNT_ID, RULE_ID, [event], confirm=True, now=NOW)
        sent = json.loads(post.calls[0].request.content)["elements"][0]
        assert sent["user"] == {"userIds": [], "lead": LEAD_URN}
        assert sent["conversion"] == RULE_URN
        assert result.result == "applied"
        assert LEAD_URN not in settings.audit_log_path.read_text()

    def test_send_events_uses_configured_version_for_hashed_names(self, tmp_path, api):
        mock_rule(api)
        settings = make_settings(tmp_path, api_version="202608")
        event = ConversionEvent(NOW, first_name="Sam", last_name="Example", hash_names=True)
        with LinkedInClient(settings) as old, pytest.raises(WriteRefusedError) as caught:
            old.conversions.send_events(ACCOUNT_ID, RULE_ID, [event], now=NOW)
        assert caught.value.reason == "invalid_input"
