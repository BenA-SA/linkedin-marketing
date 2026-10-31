"""Request shaping for Lead Sync, Conversions, Audiences, Organizations, Events.

These modules are built from the docs and LinkedIn's Postman collections;
the tests pin the exact requests so a live-API correction is a one-line diff.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import ACCOUNT_ID, ORG_ID, OTHER_ACCOUNT_ID, decoded, params, route

from linkedin_marketing import AudienceCompany, AudienceUser, ConversionEvent, hash_email
from linkedin_marketing.errors import NotAllowedError, WriteRefusedError
from linkedin_marketing.resources.organizations import escape_little

ACCOUNT_URN = f"urn:li:sponsoredAccount:{ACCOUNT_ID}"
NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
EMAIL_HASH = hash_email("Someone@Example.com")


class TestLeads:
    def test_forms_by_account_owner(self, client, api):
        call = route(
            api,
            "GET",
            "/leadForms",
            json={
                "elements": [
                    {
                        "id": 7,
                        "name": "Guide form",
                        "state": "PUBLISHED",
                        "content": {"questions": [{"questionId": 1, "predefinedField": "EMAIL"}]},
                    }
                ]
            },
        )
        forms = client.leads.forms(account_id=ACCOUNT_ID)
        assert forms[0].questions[0]["predefined_field"] == "EMAIL"
        sent = params(call.calls[0])
        assert sent["q"] == "owner"
        assert sent["owner"] == f"(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{ACCOUNT_ID})"

    def test_needs_exactly_one_owner(self, client):
        with pytest.raises(ValueError):
            client.leads.forms()
        with pytest.raises(ValueError):
            client.leads.forms(account_id=ACCOUNT_ID, organization_id=ORG_ID)

    def test_responses_query_and_answer_mapping(self, client, api):
        call = route(
            api,
            "GET",
            "/leadFormResponses",
            json={
                "elements": [
                    {
                        "id": "abc-1",
                        "submittedAt": 1793448000000,
                        "leadType": "SPONSORED",
                        "testLead": True,
                        "form": {
                            "content": {
                                "questions": [{"questionId": 1, "predefinedField": "EMAIL"}]
                            }
                        },
                        "formResponse": {
                            "answers": [
                                {
                                    "questionId": 1,
                                    "answerDetails": {
                                        "textQuestionAnswer": {"answer": "a@example.com"}
                                    },
                                },
                                {
                                    "questionId": 2,
                                    "answerDetails": {"multipleChoiceAnswer": {"options": [1]}},
                                },
                            ]
                        },
                    }
                ]
            },
        )
        leads = client.leads.responses(
            organization_id=ORG_ID,
            lead_type="company",
            form_id=55,
            submitted_after=datetime(2026, 10, 1, tzinfo=UTC),
            submitted_before=datetime(2026, 10, 2, tzinfo=UTC),
        )
        assert leads[0].answers == {"EMAIL": "a@example.com", "2": [1]}
        sent = decoded(call.calls[0])
        assert sent["owner"] == f"(organization:urn:li:organization:{ORG_ID})"
        assert sent["leadType"] == "(leadType:COMPANY)"
        assert (
            sent["versionedLeadGenFormUrn"]
            == "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:55,1)"
        )
        assert sent["submittedAtTimeRange"] == "(start:1790812800000,end:1790899200000)"
        assert sent["fields"].startswith("ownerInfo,")
        assert params(call.calls[0])["versionedLeadGenFormUrn"].startswith(
            "urn%3Ali%3AversionedLeadGenForm%3A%28"
        )

    def test_bad_lead_type(self, client):
        with pytest.raises(ValueError):
            client.leads.responses(account_id=ACCOUNT_ID, lead_type="ORGANIZATION")

    def test_form_owned_elsewhere_refused(self, client, api):
        route(
            api,
            "GET",
            "/leadForms/9",
            json={
                "id": 9,
                "owner": {"sponsoredAccount": f"urn:li:sponsoredAccount:{OTHER_ACCOUNT_ID}"},
            },
        )
        with pytest.raises(NotAllowedError):
            client.leads.form(9)


class TestConversions:
    def rule(self, api, enabled=True):
        return route(
            api,
            "GET",
            "/conversions/44",
            json={"id": 44, "name": "CRM lead", "type": "LEAD", "enabled": enabled},
        )

    def test_rules_by_account(self, client, api):
        call = route(
            api, "GET", "/conversions?", json={"elements": [{"id": 44, "name": "CRM lead"}]}
        )
        assert client.conversions.rules(ACCOUNT_ID)[0].id == 44
        assert params(call.calls[0])["account"] == f"urn%3Ali%3AsponsoredAccount%3A{ACCOUNT_ID}"

    def test_send_events_dry_run_hashes_and_never_sends(self, client, api, settings):
        self.rule(api)
        post = route(api, "POST", "/conversionEvents", status=201)
        event = ConversionEvent(
            happened_at=NOW - timedelta(days=1),
            email=" Someone@Example.com ",
            value="99.5",
            currency="GBP",
            event_id="order-1",
            first_name="Sam",
            last_name="Example",
        )
        result = client.conversions.send_events(ACCOUNT_ID, 44, [event], now=NOW)
        assert result.dry_run
        assert not post.called
        sent = result.request["json"]["elements"][0]
        assert sent["conversion"] == "urn:lla:llaPartnerConversion:44"
        assert sent["user"]["userIds"] == [{"idType": "SHA256_EMAIL", "idValue": EMAIL_HASH}]
        assert sent["conversionValue"] == {"currencyCode": "GBP", "amount": "99.50"}
        assert sent["eventId"] == "order-1"
        assert "Someone@Example.com".lower() not in json.dumps(result.to_dict()).lower()
        assert result.request["headers"]["X-RestLi-Method"] == "BATCH_CREATE"

    def test_send_events_confirmed_audit_has_counts_not_people(self, client, api, settings):
        self.rule(api)
        post = route(api, "POST", "/conversionEvents", status=201)
        event = ConversionEvent(happened_at=NOW, email="someone@example.com")
        client.conversions.send_events(ACCOUNT_ID, 44, [event], confirm=True, now=NOW)
        assert post.called
        audit = settings.audit_log_path.read_text()
        assert EMAIL_HASH not in audit
        assert '"event_count": 1' in audit

    @pytest.mark.parametrize(
        "event",
        [
            ConversionEvent(happened_at=NOW - timedelta(days=91), email="a@example.com"),
            ConversionEvent(happened_at=NOW),
        ],
    )
    def test_invalid_events_refused(self, client, api, event):
        self.rule(api)
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.send_events(ACCOUNT_ID, 44, [event], now=NOW)
        assert caught.value.reason == "invalid_input"

    def test_disabled_rule_and_batch_size(self, client, api):
        self.rule(api, enabled=False)
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.send_events(
                ACCOUNT_ID, 44, [ConversionEvent(NOW, "a@example.com")], now=NOW
            )
        assert caught.value.reason == "rule_disabled"
        with pytest.raises(WriteRefusedError) as caught:
            client.conversions.send_events(ACCOUNT_ID, 44, [], now=NOW)
        assert caught.value.reason == "batch_too_large"

    def test_create_rule_dry_run(self, client):
        result = client.conversions.create_rule(ACCOUNT_ID, "Qualified lead", "qualified_lead")
        assert result.request["json"] == {
            "name": "Qualified lead",
            "account": ACCOUNT_URN,
            "conversionMethod": "CONVERSIONS_API",
            "type": "QUALIFIED_LEAD",
            "postClickAttributionWindowSize": 30,
            "viewThroughAttributionWindowSize": 7,
        }


class TestAudiences:
    def segment(self, api, segment_type="USER", account=ACCOUNT_ID):
        return route(
            api,
            "GET",
            "/dmpSegments/66",
            json={
                "id": 66,
                "name": "CRM",
                "type": segment_type,
                "account": f"urn:li:sponsoredAccount:{account}",
                "destinations": [
                    {"destination": "LINKEDIN", "status": "READY", "matchedCount": 400}
                ],
            },
        )

    def test_segments_by_account(self, client, api):
        call = route(
            api,
            "GET",
            "/dmpSegments?",
            json={
                "elements": [{"id": 66, "type": "USER", "destinations": [{"status": "BUILDING"}]}]
            },
        )
        assert client.audiences.segments(ACCOUNT_ID)[0].destination_status == "BUILDING"
        assert params(call.calls[0])["account"] == f"urn%3Ali%3AsponsoredAccount%3A{ACCOUNT_ID}"

    def test_create_segment_body(self, client):
        result = client.audiences.create_segment(ACCOUNT_ID, "CRM contacts")
        assert result.request["json"] == {
            "name": "CRM contacts",
            "sourcePlatform": "DIRECT_API",
            "account": ACCOUNT_URN,
            "type": "USER",
            "destinations": [{"destination": "LINKEDIN"}],
        }

    def test_add_users_batch(self, client, api):
        self.segment(api)
        post = route(api, "POST", "/dmpSegments/66/users", json={"elements": [{"status": 201}]})
        result = client.audiences.add_users(
            66,
            [AudienceUser(email="someone@example.com", first_name="Sam", last_name="Example")],
            confirm=True,
        )
        sent = post.calls[0].request
        assert sent.headers["X-RestLi-Method"] == "BATCH_CREATE"
        body = json.loads(sent.content)
        assert body["elements"][0] == {
            "action": "ADD",
            "userIds": [{"idType": "SHA256_EMAIL", "idValue": hash_email("someone@example.com")}],
            "firstName": "Sam",
            "lastName": "Example",
        }
        assert result.response == {"elements": [{"status": 201}]}

    def test_remove_companies(self, client, api):
        self.segment(api, segment_type="COMPANY")
        result = client.audiences.remove_companies(
            66,
            [AudienceCompany(name="Example Ltd", website_domain="example.com", organization_id=5)],
        )
        assert result.request["json"]["elements"][0] == {
            "action": "REMOVE",
            "companyName": "Example Ltd",
            "companyWebsiteDomain": "example.com",
            "organizationUrn": "urn:li:organization:5",
        }

    def test_type_mismatch_and_foreign_segment(self, client, api):
        self.segment(api, segment_type="COMPANY")
        with pytest.raises(WriteRefusedError):
            client.audiences.add_users(66, [AudienceUser(email="a@example.com")])
        api.routes.clear()
        self.segment(api, account=OTHER_ACCOUNT_ID)
        with pytest.raises(NotAllowedError):
            client.audiences.segment(66)

    def test_member_needs_identifier(self, client, api):
        self.segment(api)
        with pytest.raises(WriteRefusedError):
            client.audiences.add_users(66, [AudienceUser(title="CEO")])


class TestOrganizations:
    ORG = f"urn%3Ali%3Aorganization%3A{ORG_ID}"

    def test_administered_filters_allow_list(self, client, api):
        call = route(
            api,
            "GET",
            "/organizationAcls",
            json={
                "elements": [
                    {
                        "organization": f"urn:li:organization:{ORG_ID}",
                        "role": "ADMINISTRATOR",
                        "state": "APPROVED",
                    },
                    {"organizationTarget": "urn:li:organization:999", "role": "ADMINISTRATOR"},
                ]
            },
        )
        orgs = client.organizations.administered()
        assert [o["organization_id"] for o in orgs] == [str(ORG_ID)]
        assert params(call.calls[0])["q"] == "roleAssignee"

    def test_org_outside_allow_list(self, client):
        with pytest.raises(NotAllowedError):
            client.organizations.posts(999)

    def test_posts_finder(self, client, api):
        call = route(
            api, "GET", "/posts", json={"elements": [{"id": "urn:li:share:1", "commentary": "Hi"}]}
        )
        posts = client.organizations.posts(ORG_ID, limit=5)
        assert posts[0].commentary == "Hi"
        assert call.calls[0].request.headers["X-RestLi-Method"] == "FINDER"
        assert params(call.calls[0])["author"] == self.ORG

    def test_social_metadata_and_follower_count(self, client, api):
        route(
            api,
            "GET",
            "/socialMetadata",
            json={
                "results": {
                    "urn:li:share:1": {
                        "reactionSummaries": {"LIKE": {"count": 3}, "CELEBRATION": {"count": 1}},
                        "commentSummary": {"count": 2, "topLevelCount": 1},
                    }
                }
            },
        )
        meta = client.organizations.social_metadata(["urn:li:share:1"])
        assert meta["urn:li:share:1"]["reaction_total"] == 4
        call = route(api, "GET", f"/networkSizes/{self.ORG}", json={"firstDegreeSize": 1234})
        assert client.organizations.follower_count(ORG_ID) == 1234
        assert params(call.calls[0])["edgeType"] == "COMPANY_FOLLOWED_BY_MEMBER"

    def test_statistics_time_intervals(self, client, api):
        call = route(api, "GET", "/organizationalEntityFollowerStatistics", json={"elements": []})
        client.organizations.follower_statistics(
            ORG_ID, date(2026, 9, 1), date(2026, 10, 1), "month"
        )
        assert params(call.calls[0])["timeIntervals"] == (
            "(timeRange:(start:1788220800000,end:1790812800000),timeGranularityType:MONTH)"
        )
        page = route(api, "GET", "/organizationPageStatistics", json={"elements": []})
        client.organizations.page_statistics(ORG_ID)
        assert "timeIntervals" not in params(page.calls[0])

    def test_share_statistics_per_post_lifetime_only(self, client, api):
        call = route(api, "GET", "/organizationalEntityShareStatistics", json={"elements": []})
        client.organizations.share_statistics(
            ORG_ID, post_urns=["urn:li:share:1", "urn:li:ugcPost:2"]
        )
        sent = params(call.calls[0])
        assert sent["shares"] == "List(urn%3Ali%3Ashare%3A1)"
        assert sent["ugcPosts"] == "List(urn%3Ali%3AugcPost%3A2)"
        with pytest.raises(ValueError):
            client.organizations.share_statistics(
                ORG_ID, date(2026, 1, 1), post_urns=["urn:li:share:1"]
            )

    def test_create_post_dry_run_escapes_and_warns(self, client, api):
        post = route(api, "POST", "/posts", status=201, headers={"x-restli-id": "urn:li:share:9"})
        result = client.organizations.create_post(
            ORG_ID, "New guide (free) #data", link_url="https://example.com"
        )
        assert not post.called
        body = result.request["json"]
        assert body["author"] == f"urn:li:organization:{ORG_ID}"
        assert body["commentary"] == "New guide \\(free\\) \\#data"
        assert body["content"]["article"]["source"] == "https://example.com"
        assert "PUBLIC" in result.warning

    def test_create_post_confirmed_returns_urn(self, client, api):
        route(api, "POST", "/posts", status=201, headers={"x-restli-id": "urn:li:share:9"})
        result = client.organizations.create_post(ORG_ID, "Hello", confirm=True)
        assert result.created_id == "urn:li:share:9"

    def test_escape_little(self):
        assert escape_little("a_b*c") == "a\\_b\\*c"


class TestEvents:
    def test_by_organizer(self, client, api):
        call = route(
            api,
            "GET",
            "/events",
            json={
                "elements": [
                    {
                        "id": 3,
                        "name": {"localized": {"en_US": "Webinar"}},
                        "organizer": f"urn:li:organization:{ORG_ID}",
                    }
                ]
            },
        )
        events = client.events.by_organizer(ORG_ID, "past")
        assert events[0].name == "Webinar"
        sent = params(call.calls[0])
        assert sent["q"] == "eventsByOrganizer"
        assert sent["timeBasedFilter"] == "(lifeCycleState:PAST)"
        assert sent["sortOrder"] == "END_TIME_DESC"

    def test_get_checks_organizer(self, client, api):
        route(api, "GET", "/events/3", json={"id": 3, "organizer": "urn:li:organization:999"})
        with pytest.raises(NotAllowedError):
            client.events.get(3)
