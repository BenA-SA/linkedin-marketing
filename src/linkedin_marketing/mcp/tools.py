"""MCP tool definitions: thin wrappers that call the library and return dicts.

No API logic lives here. Each ``register_*`` function adds one module's
tools; ``server.build_server`` calls only those for enabled modules. Write
tools pass ``confirm`` straight through to the library, whose safety layer
does the dry run, read-only refusal, allow-lists, caps and audit log.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Literal

from linkedin_marketing.client import LinkedInClient
from linkedin_marketing.modules import MODULES
from linkedin_marketing.resources.audiences import AudienceCompany, AudienceUser
from linkedin_marketing.resources.conversions import ConversionEvent

ClientFactory = Callable[[], LinkedInClient]
DEFAULT_LOOKBACK_DAYS = 30
WRITE_NOTE = (
    " DRY RUN unless confirm=True: the dry run returns the exact request. Only pass "
    "confirm=True after the user has approved that exact change. Confirmed writes "
    "need LINKEDIN_READ_ONLY=false and are appended to the local audit log."
)


def _experimental(module: str) -> str:
    return (
        "[EXPERIMENTAL: built from docs, not live-tested] " if MODULES[module].experimental else ""
    )


def _date(value: str | None, default: date | None = None) -> date | None:
    return date.fromisoformat(value) if value else default


def _start(value: str | None) -> date:
    return _date(value, date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS))  # type: ignore[return-value]


def _account(client: LinkedInClient, account_id: int | None) -> int:
    """The given account, or the first allow-listed one."""
    if account_id is not None:
        return int(account_id)
    allowed = client.settings.allowed_ad_account_ids
    if not allowed:
        raise ValueError("Pass account_id (no LINKEDIN_ALLOWED_AD_ACCOUNT_IDS default is set).")
    return allowed[0]


def _organization(client: LinkedInClient, organization_id: int | None) -> int:
    if organization_id is not None:
        return int(organization_id)
    allowed = client.settings.allowed_organization_ids
    if not allowed:
        raise ValueError(
            "Pass organization_id (no LINKEDIN_ALLOWED_ORGANIZATION_IDS default is set)."
        )
    return allowed[0]


def _dicts(items) -> list[dict]:
    return [item.to_dict() for item in items]


def _add(server, fn, *, write: bool = False, description: str = "") -> None:
    """Register ``fn`` with read/write annotations understood by MCP clients."""
    from mcp.types import ToolAnnotations

    annotations = ToolAnnotations(readOnlyHint=not write, destructiveHint=write, openWorldHint=True)
    server.add_tool(
        fn, name=fn.__name__, description=description or fn.__doc__, annotations=annotations
    )


def register_ads(server, get_client: ClientFactory) -> None:
    """Account structure, targeting/forecasting, and the three guarded writes."""
    _register_ads_structure(server, get_client)
    _register_ads_planning(server, get_client)
    _register_ads_writes(server, get_client)


def _register_ads_structure(server, get_client: ClientFactory) -> None:
    def li_list_ad_accounts(limit: int = 50) -> list[dict]:
        """List ad accounts the token can access (only allow-listed ones if a list is set)."""
        return _dicts(get_client().ads.accounts.list(limit=limit))

    def li_list_account_users(account_id: int | None = None) -> list[dict]:
        """List members and their roles on an ad account."""
        client = get_client()
        return _dicts(client.ads.accounts.users(_account(client, account_id)))

    def li_list_campaign_groups(
        account_id: int | None = None, statuses: list[str] | None = None
    ) -> list[dict]:
        """List campaign groups (default statuses ACTIVE, PAUSED, DRAFT)."""
        client = get_client()
        groups = client.ads.campaign_groups.list(
            _account(client, account_id), **_statuses(statuses)
        )
        return _dicts(groups)

    def li_list_campaigns(
        account_id: int | None = None,
        statuses: list[str] | None = None,
        campaign_group_id: int | None = None,
    ) -> list[dict]:
        """List campaigns (ad sets) with budgets, schedule and a targeting summary."""
        client = get_client()
        campaigns = client.ads.campaigns.list(
            _account(client, account_id), campaign_group_id=campaign_group_id, **_statuses(statuses)
        )
        return _dicts(campaigns)

    def li_list_creatives(
        account_id: int | None = None,
        campaign_ids: list[int] | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        """List creatives (ads), optionally only those in the given campaigns."""
        client = get_client()
        creatives = client.ads.creatives.list(
            _account(client, account_id), campaign_ids or [], **_statuses(statuses)
        )
        return _dicts(creatives)

    for fn in (
        li_list_ad_accounts,
        li_list_account_users,
        li_list_campaign_groups,
        li_list_campaigns,
        li_list_creatives,
    ):
        _add(server, fn)


def _register_ads_planning(server, get_client: ClientFactory) -> None:
    def li_search_targeting(facet: str, query: str) -> list[dict]:
        """Find targeting URNs by text, e.g. facet='titles', query='finance director'."""
        return _dicts(get_client().ads.targeting.typeahead(facet, query))

    def li_list_targeting_facets() -> list[dict]:
        """List every targeting facet and its available finders."""
        return get_client().ads.targeting.facets()

    def li_audience_count(targeting_criteria: dict) -> dict:
        """Approximate audience size for LinkedIn targetingCriteria JSON (0 if under 300)."""
        return get_client().ads.targeting.audience_count(targeting_criteria)

    def li_preview_creative(creative_id: int, account_id: int | None = None) -> list[dict]:
        """Rendered preview HTML (iframe, valid ~3 hours) for an existing creative."""
        client = get_client()
        return client.ads.previews.creative(_account(client, account_id), creative_id)

    def li_budget_pricing(
        targeting_criteria: dict,
        currency: str,
        bid_type: Literal["CPM", "CPC", "CPV"] = "CPM",
        account_id: int | None = None,
    ) -> dict:
        """Suggested bid and budget limits for a would-be Sponsored Content campaign."""
        client = get_client()
        return client.ads.forecasting.budget_pricing(
            _account(client, account_id), targeting_criteria, currency, bid_type
        )

    for fn in (
        li_search_targeting,
        li_list_targeting_facets,
        li_audience_count,
        li_preview_creative,
        li_budget_pricing,
    ):
        _add(server, fn)


def _register_ads_writes(server, get_client: ClientFactory) -> None:
    def li_set_campaign_status(
        campaign_id: int,
        status: Literal["ACTIVE", "PAUSED"],
        confirm: bool = False,
        account_id: int | None = None,
    ) -> dict:
        client = get_client()
        return client.ads.campaigns.set_status(
            _account(client, account_id), campaign_id, status, confirm
        ).to_dict()

    def li_set_campaign_group_status(
        campaign_group_id: int,
        status: Literal["ACTIVE", "PAUSED"],
        confirm: bool = False,
        account_id: int | None = None,
    ) -> dict:
        client = get_client()
        groups = client.ads.campaign_groups
        return groups.set_status(
            _account(client, account_id), campaign_group_id, status, confirm
        ).to_dict()

    def li_set_campaign_daily_budget(
        campaign_id: int, daily_budget: str, confirm: bool = False, account_id: int | None = None
    ) -> dict:
        client = get_client()
        campaigns = client.ads.campaigns
        return campaigns.set_daily_budget(
            _account(client, account_id), campaign_id, daily_budget, confirm
        ).to_dict()

    _add(
        server,
        li_set_campaign_status,
        write=True,
        description="Pause or resume one campaign (ACTIVE <-> PAUSED only)." + WRITE_NOTE,
    )
    _add(
        server,
        li_set_campaign_group_status,
        write=True,
        description="Pause or resume one campaign group (ACTIVE <-> PAUSED only)." + WRITE_NOTE,
    )
    _add(
        server,
        li_set_campaign_daily_budget,
        write=True,
        description="Change one campaign's daily budget (e.g. '30.00', campaign currency), "
        "capped by LINKEDIN_MAX_DAILY_BUDGET." + WRITE_NOTE,
    )


def _statuses(statuses: list[str] | None) -> dict:
    return {"statuses": statuses} if statuses else {}


def register_reporting(server, get_client: ClientFactory) -> None:
    def li_ad_analytics(
        start_date: str | None = None,
        end_date: str | None = None,
        pivot: str = "CAMPAIGN",
        time_granularity: Literal["ALL", "DAILY", "MONTHLY", "YEARLY"] = "ALL",
        metrics: list[str] | None = None,
        campaign_ids: list[int] | None = None,
        campaign_group_ids: list[int] | None = None,
        account_id: int | None = None,
    ) -> dict:
        """Spend, impressions, clicks, leads with CTR/CPC/CPM/CPL per pivot value.

        Dates are ISO (YYYY-MM-DD, UTC, inclusive); default the last 30 days.
        pivot: CAMPAIGN, CREATIVE, CAMPAIGN_GROUP, ACCOUNT, CONVERSION, or a
        MEMBER_* demographic pivot. Use ALL for totals (daily rows carry privacy
        noise). metrics are adAnalytics field names (max 20).
        """
        client = get_client()
        report = client.reporting.analytics(
            _account(client, account_id),
            _start(start_date),
            _date(end_date),
            pivot,
            time_granularity,
            metrics,
            campaign_ids or [],
            campaign_group_ids or [],
        )
        return report.to_dict()

    def li_ad_demographics(
        pivot: str = "MEMBER_JOB_FUNCTION",
        start_date: str | None = None,
        end_date: str | None = None,
        campaign_ids: list[int] | None = None,
        account_id: int | None = None,
    ) -> dict:
        """Who saw and clicked, by MEMBER_JOB_FUNCTION, MEMBER_SENIORITY, MEMBER_INDUSTRY,
        MEMBER_COMPANY_SIZE, MEMBER_COUNTRY_V2, MEMBER_JOB_TITLE or MEMBER_COMPANY,
        with readable names. Values are approximate; under-3 counts are dropped."""
        client = get_client()
        report = client.reporting.demographics(
            _account(client, account_id),
            _start(start_date),
            pivot,
            _date(end_date),
            campaign_ids=campaign_ids or [],
        )
        return report.to_dict()

    _add(server, li_ad_analytics)
    _add(server, li_ad_demographics)


def register_ad_library(server, get_client: ClientFactory) -> None:
    def li_search_ad_library(
        keyword: str | None = None,
        advertiser: str | None = None,
        payer: str | None = None,
        countries: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 25,
        start: int = 0,
    ) -> dict:
        """Search LinkedIn's public Ad Library (any advertiser). Needs keyword, advertiser
        or payer. countries are ISO-2 codes (e.g. GB). Returns ad URL, advertiser, payer,
        format and targeting; resume with start=next_start. The daily quota is small."""
        page = get_client().ad_library.search(
            keyword,
            advertiser,
            payer,
            countries or [],
            _date(start_date),
            _date(end_date),
            limit,
            start,
        )
        return page.to_dict()

    _add(server, li_search_ad_library)


def register_leads(server, get_client: ClientFactory) -> None:
    note = _experimental("leads")

    def li_list_lead_forms(
        account_id: int | None = None, organization_id: int | None = None
    ) -> list[dict]:
        client = get_client()
        if organization_id is None:
            account_id = _account(client, account_id)
        return _dicts(client.leads.forms(account_id, organization_id))

    def li_list_leads(
        account_id: int | None = None,
        organization_id: int | None = None,
        lead_type: Literal["SPONSORED", "COMPANY", "EVENT", "ORGANIZATION_PRODUCT"] = "SPONSORED",
        form_id: int | None = None,
        submitted_after: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        client = get_client()
        if organization_id is None:
            account_id = _account(client, account_id)
        after = datetime.fromisoformat(submitted_after) if submitted_after else None
        leads = client.leads.responses(
            account_id, organization_id, lead_type, form_id, submitted_after=after, limit=limit
        )
        return _dicts(leads)

    _add(
        server,
        li_list_lead_forms,
        description=note + "List lead gen forms owned by an ad account or page.",
    )
    _add(
        server,
        li_list_leads,
        description=note + "List lead form responses (leads) with answers "
        "keyed by field. Contains personal data: handle accordingly.",
    )


def register_conversions(server, get_client: ClientFactory) -> None:
    note = _experimental("conversions")

    def li_list_conversion_rules(account_id: int | None = None) -> list[dict]:
        client = get_client()
        return _dicts(client.conversions.rules(_account(client, account_id)))

    def li_create_conversion_rule(
        name: str, rule_type: str = "LEAD", confirm: bool = False, account_id: int | None = None
    ) -> dict:
        client = get_client()
        result = client.conversions.create_rule(
            _account(client, account_id), name, rule_type, confirm=confirm
        )
        return result.to_dict()

    def li_send_conversion_events(
        rule_id: int, events: list[dict], confirm: bool = False, account_id: int | None = None
    ) -> dict:
        client = get_client()
        parsed = [
            ConversionEvent(
                **{**event, "happened_at": datetime.fromisoformat(event["happened_at"])}
            )
            for event in events
        ]
        result = client.conversions.send_events(
            _account(client, account_id), rule_id, parsed, confirm
        )
        return result.to_dict()

    _add(
        server,
        li_list_conversion_rules,
        description=note + "List conversion rules on an ad account.",
    )
    _add(
        server,
        li_create_conversion_rule,
        write=True,
        description=note + "Create a CONVERSIONS_API conversion rule." + WRITE_NOTE,
    )
    _add(
        server,
        li_send_conversion_events,
        write=True,
        description=note
        + "Send server-side conversion events to a rule. Each event: happened_at (ISO, past "
        "90 days) plus email (hashed locally) or li_fat_id or first_name+last_name; optional "
        "value, currency, event_id (dedup), company, title, country_code." + WRITE_NOTE,
    )


def register_audiences(server, get_client: ClientFactory) -> None:
    note = _experimental("audiences")

    def li_list_audiences(account_id: int | None = None) -> list[dict]:
        client = get_client()
        return _dicts(client.audiences.segments(_account(client, account_id)))

    def li_create_audience(
        name: str,
        segment_type: Literal["USER", "COMPANY"] = "USER",
        confirm: bool = False,
        account_id: int | None = None,
    ) -> dict:
        client = get_client()
        result = client.audiences.create_segment(
            _account(client, account_id), name, segment_type, confirm=confirm
        )
        return result.to_dict()

    def li_update_audience_contacts(
        segment_id: int,
        action: Literal["ADD", "REMOVE"],
        contacts: list[dict],
        confirm: bool = False,
    ) -> dict:
        audiences = get_client().audiences
        users = [AudienceUser(**contact) for contact in contacts]
        method = audiences.add_users if action == "ADD" else audiences.remove_users
        return method(segment_id, users, confirm).to_dict()

    def li_update_audience_companies(
        segment_id: int,
        action: Literal["ADD", "REMOVE"],
        companies: list[dict],
        confirm: bool = False,
    ) -> dict:
        audiences = get_client().audiences
        rows = [AudienceCompany(**company) for company in companies]
        method = audiences.add_companies if action == "ADD" else audiences.remove_companies
        return method(segment_id, rows, confirm).to_dict()

    _add(
        server,
        li_list_audiences,
        description=note + "List Matched Audiences (DMP segments) with match status.",
    )
    _add(
        server,
        li_create_audience,
        write=True,
        description=note + "Create a streaming Matched Audience segment." + WRITE_NOTE,
    )
    _add(
        server,
        li_update_audience_contacts,
        write=True,
        description=note
        + "Add or remove contacts (email hashed locally, or first_name+last_name; optional "
        "title, company, country)." + WRITE_NOTE,
    )
    _add(
        server,
        li_update_audience_companies,
        write=True,
        description=note
        + "Add or remove companies (name, website_domain, email_domain, organization_id or "
        "page_url; optional country)." + WRITE_NOTE,
    )


def register_organizations(server, get_client: ClientFactory) -> None:
    note = _experimental("organizations")

    def li_list_my_organizations() -> list[dict]:
        return get_client().organizations.administered()

    def li_list_organization_posts(
        organization_id: int | None = None, limit: int = 20
    ) -> list[dict]:
        client = get_client()
        return _dicts(client.organizations.posts(_organization(client, organization_id), limit))

    def li_post_engagement(post_urns: list[str]) -> dict:
        return get_client().organizations.social_metadata(post_urns)

    def li_organization_statistics(
        kind: Literal["followers", "page", "shares"] = "followers",
        organization_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: Literal["DAY", "WEEK", "MONTH"] = "DAY",
    ) -> dict:
        client = get_client()
        orgs = client.organizations
        org_id = _organization(client, organization_id)
        method = {
            "followers": orgs.follower_statistics,
            "page": orgs.page_statistics,
            "shares": orgs.share_statistics,
        }[kind]
        elements = method(org_id, _date(start_date), _date(end_date), granularity)
        return {"follower_count": orgs.follower_count(org_id), "elements": elements}

    def li_create_organization_post(
        text: str,
        link_url: str | None = None,
        link_title: str | None = None,
        confirm: bool = False,
        organization_id: int | None = None,
    ) -> dict:
        client = get_client()
        result = client.organizations.create_post(
            _organization(client, organization_id), text, link_url, link_title, confirm=confirm
        )
        return result.to_dict()

    _add(server, li_list_my_organizations, description=note + "Pages the member administers.")
    _add(server, li_list_organization_posts, description=note + "Recent posts by a page.")
    _add(
        server, li_post_engagement, description=note + "Reaction and comment counts for post URNs."
    )
    _add(
        server,
        li_organization_statistics,
        description=note
        + "Follower, page-view or share statistics (lifetime, or per period with start_date).",
    )
    _add(
        server,
        li_create_organization_post,
        write=True,
        description=note + "Publish a PUBLIC post as the page (text, optional link)." + WRITE_NOTE,
    )


def register_events(server, get_client: ClientFactory) -> None:
    note = _experimental("events")

    def li_list_events(
        organization_id: int | None = None,
        lifecycle_state: Literal["UPCOMING", "ONGOING", "PAST"] = "UPCOMING",
    ) -> list[dict]:
        client = get_client()
        return _dicts(
            client.events.by_organizer(_organization(client, organization_id), lifecycle_state)
        )

    _add(server, li_list_events, description=note + "LinkedIn Events organised by a page.")


REGISTRARS = {
    "ads": register_ads,
    "reporting": register_reporting,
    "ad_library": register_ad_library,
    "leads": register_leads,
    "conversions": register_conversions,
    "audiences": register_audiences,
    "organizations": register_organizations,
    "events": register_events,
}
