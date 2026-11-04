"""Conversions API: rules, campaign associations and server-side events. EXPERIMENTAL.

Needs the "Conversions API" product, scopes ``rw_conversions`` and ``r_ads``,
and an ad-account role above VIEWER. Built from the docs and LinkedIn's
official Postman collection ("Conversions API"); not yet verified against the
live API.

LinkedIn's three steps, all covered here:

1. **Create a rule** (``POST /conversions``, or ``BATCH_CREATE`` up to 100).
   ``auto_association`` adds ``?autoAssociationType=ALL_CAMPAIGNS`` (up to 200
   campaigns) or ``OBJECTIVE_BASED`` (campaigns whose objective maps to the
   rule type). Without it no campaign is associated.
2. **Associate campaigns** (``PUT /campaignConversions/(campaign:..,conversion:..)``,
   or ``BATCH_UPDATE`` with ``ids=List(...)``). Only associated campaigns get
   conversions attributed, so a rule with no campaigns reports nothing.
3. **Stream events** (``POST /conversionEvents`` with ``BATCH_CREATE``, up to
   5,000 per call; one invalid element fails the whole batch).
   ``conversionHappenedAt`` must be within the past 90 days. ``event_id`` is
   LinkedIn's de-duplication key.

Identifiers: an event must carry at least one ``userIds`` entry
(``SHA256_EMAIL``, ``LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID``,
``PLAINTEXT_IP_ADDRESS``), or a first and last name (``userInfo``), or a Lead
Gen Form response URN (``lead``), or an ``externalIds`` value. ``userIds`` is
always sent, as ``[]`` when the member is identified only by the others,
because LinkedIn answers 422 when it is missing. Emails are SHA-256 hashed
locally, and names too when ``hash_names`` is set (``hashedFirstName`` /
``hashedLastName``, LinkedIn-Version 202609 or later). The audit log records
only counts, never people.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/conversions-api
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/conversions-api-schema
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/conversion-tracking
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from linkedin_marketing import restli
from linkedin_marketing.config import DEFAULT_API_VERSION
from linkedin_marketing.errors import CampaignNotInAccountError, WriteRefusedError
from linkedin_marketing.hashing import hash_email, hash_name, is_sha256_hex
from linkedin_marketing.models import CampaignConversion, ConversionRule, to_money
from linkedin_marketing.resources.ads import ALL_CAMPAIGN_STATUSES, Campaigns
from linkedin_marketing.resources.base import Resource, require_choice
from linkedin_marketing.safety import Guard, WritePlan, WriteResult
from linkedin_marketing.transport import Request, Transport

MAX_EVENTS_PER_BATCH = 5000
MAX_RULES_PER_BATCH = 100
MAX_CAMPAIGNS_PER_ASSOCIATION = 50
ASSOCIATIONS_PAGE_SIZE = 100
MAX_EVENT_AGE = timedelta(days=90)
LEAD_URN_PREFIX = "urn:li:leadGenFormResponse:"

RULE_TYPES = (
    "ADD_BILLING_INFO",
    "ADD_TO_CART",
    "ADD_TO_LIST",
    "AD_CLICK",
    "AD_VIEW",
    "BOOK_APPOINTMENT",
    "COMPLETE_SIGNUP",
    "CONTACT",
    "DONATE",
    "DOWNLOAD",
    "INSTALL",
    "INVITE",
    "KEY_PAGE_VIEW",
    "LEAD",
    "LOGIN",
    "MARKETING_QUALIFIED_LEAD",
    "OTHER",
    "OUTBOUND_CLICK",
    "PHONE_CALL",
    "PURCHASE",
    "QUALIFIED_LEAD",
    "REQUEST_QUOTE",
    "SALES_QUALIFIED_LEAD",
    "SAVE",
    "SCHEDULE",
    "SEARCH",
    "SHARE",
    "SIGN_UP",
    "START_CHECKOUT",
    "START_TRIAL",
    "SUBMIT_APPLICATION",
    "SUBSCRIBE",
    "VIEW_CONTENT",
    "VIEW_VIDEO",
)
RULE_TYPE_MIN_VERSION = {"MARKETING_QUALIFIED_LEAD": "202608", "SALES_QUALIFIED_LEAD": "202608"}
HASHED_NAMES_MIN_VERSION = "202609"

POST_CLICK_WINDOWS = (1, 7, 28, 30, 90)
VIEW_THROUGH_WINDOWS = (1, 7, 30, 90)
LONG_WINDOWS = (180, 365)
LONG_WINDOW_RULE_TYPES = (
    "SUBMIT_APPLICATION",
    "PURCHASE",
    "ADD_TO_CART",
    "QUALIFIED_LEAD",
    "LEAD",
    "MARKETING_QUALIFIED_LEAD",
    "SALES_QUALIFIED_LEAD",
)
ATTRIBUTION_TYPES = ("LAST_TOUCH_BY_CAMPAIGN", "LAST_TOUCH_BY_CONVERSION")
AUTO_ASSOCIATION_TYPES = ("ALL_CAMPAIGNS", "OBJECTIVE_BASED")
NO_ASSOCIATION_WARNING = (
    "No campaigns will be associated. LinkedIn attributes conversions only to "
    "associated campaigns: pass auto_association or call associate_campaigns."
)


def conversion_urn(rule_id: int) -> str:
    return f"urn:lla:llaPartnerConversion:{int(rule_id)}"


def version_at_least(api_version: str, minimum: str) -> bool:
    """Whether ``api_version`` (``YYYYMM`` or ``YYYYMM.RR``) is ``minimum`` or later."""
    head = str(api_version)[:6]
    return head.isdigit() and int(head) >= int(minimum)


def association_key(campaign_id: int, rule_id: int) -> str:
    """``(campaign:{urn},conversion:{urn})`` with both URNs percent-encoded."""
    campaign = restli.encode(restli.campaign_urn(int(campaign_id)))
    conversion = restli.encode(conversion_urn(rule_id))
    return f"(campaign:{campaign},conversion:{conversion})"


def batch_failures(body: dict) -> list[str]:
    """Keys or indexes of the elements a batch response reports as failed.

    Covers both documented shapes: ``{"results": {key: {"status": ..}}, "errors":
    {..}}`` (batch update) and ``{"elements": [{"status": ..}]}`` (batch create).
    """
    failed = [str(key) for key in body.get("errors") or {}]
    results = body.get("results") or {}
    failed += [str(key) for key, item in results.items() if _status_failed(item)]
    elements = body.get("elements") or []
    failed += [str(index) for index, item in enumerate(elements) if _status_failed(item)]
    return failed


def _status_failed(item: object) -> bool:
    return isinstance(item, dict) and int(item.get("status") or 0) >= 400


def _window(days: int, allowed: tuple[int, ...], rule_type: str, action: str, name: str) -> int:
    """``days`` as an int, refused unless LinkedIn accepts it for ``rule_type``."""
    permitted = allowed + LONG_WINDOWS if rule_type in LONG_WINDOW_RULE_TYPES else allowed
    value = int(days)
    if value not in permitted:
        raise WriteRefusedError(
            "invalid_input",
            action,
            f"{name} for {rule_type} must be one of {', '.join(map(str, permitted))}, not {value}",
            field=name,
            rule_type=rule_type,
            days=value,
        )
    return value


def _require_type_supported(rule_type: str, api_version: str, action: str) -> None:
    minimum = RULE_TYPE_MIN_VERSION.get(rule_type)
    if minimum and not version_at_least(api_version, minimum):
        raise WriteRefusedError(
            "version_too_old",
            action,
            f"rule type {rule_type} needs LinkedIn-Version {minimum} or later, not {api_version}",
            rule_type=rule_type,
            api_version=api_version,
        )


def _auto_association_query(value: str | None, action: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    choice = require_choice(value, AUTO_ASSOCIATION_TYPES, action, "auto_association")
    return (("autoAssociationType", choice),)


def _unique_ids(ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(int(i) for i in ids))


@dataclass(frozen=True)
class ConversionRuleSpec:
    """One ``CONVERSIONS_API`` rule to create.

    Post-click windows: 1, 7, 28, 30 or 90 days; view-through: 1, 7, 30 or 90;
    180 and 365 are also allowed for both on the long-cycle types in
    ``LONG_WINDOW_RULE_TYPES``. ``attribution_type`` is LinkedIn's default
    ``LAST_TOUCH_BY_CAMPAIGN`` (counted once per campaign) or
    ``LAST_TOUCH_BY_CONVERSION`` (counted once per conversion).
    """

    name: str
    rule_type: str = "LEAD"
    post_click_window_days: int = 30
    view_through_window_days: int = 7
    attribution_type: str = "LAST_TOUCH_BY_CAMPAIGN"

    def to_api(self, account_id: int, api_version: str, action: str) -> dict:
        """The wire shape; raises ``WriteRefusedError`` on input LinkedIn would reject."""
        name = self.name.strip()
        if not name:
            raise WriteRefusedError("invalid_input", action, "name must not be empty", field="name")
        rule_type = require_choice(self.rule_type, RULE_TYPES, action, "rule_type")
        _require_type_supported(rule_type, api_version, action)
        post_click = _window(
            self.post_click_window_days,
            POST_CLICK_WINDOWS,
            rule_type,
            action,
            "post_click_window_days",
        )
        view_through = _window(
            self.view_through_window_days,
            VIEW_THROUGH_WINDOWS,
            rule_type,
            action,
            "view_through_window_days",
        )
        return {
            "name": name,
            "account": restli.account_urn(account_id),
            "conversionMethod": "CONVERSIONS_API",
            "type": rule_type,
            "postClickAttributionWindowSize": post_click,
            "viewThroughAttributionWindowSize": view_through,
            "attributionType": require_choice(
                self.attribution_type, ATTRIBUTION_TYPES, action, "attribution_type"
            ),
        }


@dataclass(frozen=True)
class ConversionEvent:
    """One offline/server-side conversion. Give at least one identifier.

    ``email`` may be plain (hashed here) or a SHA-256 hex digest.
    ``li_fat_id`` is LinkedIn's first-party click id from the landing URL.
    ``lead`` is a Lead Gen Form response URN (``urn:li:leadGenFormResponse:..``),
    which matches the member who submitted the form. ``external_id`` is your own
    id for the person (LinkedIn uses only one). ``ip_address`` is a plain IPv4
    address, which LinkedIn hashes with a salt. ``first_name`` and
    ``last_name`` count only as a pair; with ``hash_names`` (or when a value is
    already a SHA-256 digest) they are sent as ``hashedFirstName`` /
    ``hashedLastName``.
    """

    happened_at: datetime
    email: str | None = None
    li_fat_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    title: str | None = None
    country_code: str | None = None
    value: Decimal | str | None = None
    currency: str | None = None
    event_id: str | None = None
    lead: str | None = None
    external_id: str | None = None
    ip_address: str | None = None
    hash_names: bool = False

    def to_api(self, rule_id: int, now: datetime, api_version: str = DEFAULT_API_VERSION) -> dict:
        """The wire shape, validated; raises ``ValueError`` on bad input."""
        event: dict = {
            "conversion": conversion_urn(rule_id),
            "conversionHappenedAt": self._happened_ms(now),
            "user": self._user(api_version),
        }
        if self.value is not None:
            event["conversionValue"] = {
                "currencyCode": self.currency or "USD",
                "amount": str(to_money(Decimal(str(self.value)))),
            }
        if self.event_id:
            event["eventId"] = self.event_id
        return event

    def _happened_ms(self, now: datetime) -> int:
        happened = (
            self.happened_at if self.happened_at.tzinfo else self.happened_at.replace(tzinfo=UTC)
        )
        if happened > now or now - happened > MAX_EVENT_AGE:
            raise ValueError("happened_at must be within the past 90 days.")
        return int(happened.timestamp() * 1000)

    def _user(self, api_version: str) -> dict:
        user: dict = {"userIds": self._user_ids()}
        user_info = self._user_info(api_version)
        if user_info:
            user["userInfo"] = user_info
        if self.lead:
            user["lead"] = _lead_urn(self.lead)
        if self.external_id and self.external_id.strip():
            user["externalIds"] = [self.external_id.strip()]
        if not _identifies_member(user):
            raise ValueError(
                "Each event needs an email, li_fat_id, ip_address, lead, external_id, "
                "or first and last name."
            )
        return user

    def _user_ids(self) -> list[dict]:
        user_ids = []
        if self.email:
            user_ids.append({"idType": "SHA256_EMAIL", "idValue": hash_email(self.email)})
        if self.li_fat_id:
            user_ids.append(
                {"idType": "LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID", "idValue": self.li_fat_id}
            )
        if self.ip_address:
            user_ids.append({"idType": "PLAINTEXT_IP_ADDRESS", "idValue": _ipv4(self.ip_address)})
        return user_ids

    def _user_info(self, api_version: str) -> dict:
        if not (self.first_name and self.last_name):
            return {}
        info = dict(
            (
                self._name_field("firstName", self.first_name, api_version),
                self._name_field("lastName", self.last_name, api_version),
            )
        )
        optional = {
            "companyName": self.company,
            "title": self.title,
            "countryCode": self.country_code,
        }
        info.update({key: value for key, value in optional.items() if value})
        return info

    def _name_field(self, key: str, name: str, api_version: str) -> tuple[str, str]:
        """``(firstName, plain)`` or ``(hashedFirstName, digest)``."""
        if not (self.hash_names or is_sha256_hex(name.strip())):
            return key, name
        if not version_at_least(api_version, HASHED_NAMES_MIN_VERSION):
            raise ValueError(
                f"Hashed names need LinkedIn-Version {HASHED_NAMES_MIN_VERSION} or later."
            )
        return f"hashed{key[0].upper()}{key[1:]}", hash_name(name)


def _identifies_member(user: dict) -> bool:
    """LinkedIn's rule: a userId, or userInfo, or a lead, or externalIds."""
    return bool(user["userIds"]) or any(key in user for key in ("userInfo", "lead", "externalIds"))


def _lead_urn(lead: str) -> str:
    value = lead.strip()
    if not value.startswith(LEAD_URN_PREFIX) or len(value) == len(LEAD_URN_PREFIX):
        raise ValueError(f"lead must be a URN like {LEAD_URN_PREFIX}<id>.")
    return value


def _ipv4(address: str) -> str:
    try:
        return str(ipaddress.IPv4Address(address.strip()))
    except ValueError as exc:
        raise ValueError("ip_address must be an IPv4 address (LinkedIn takes no IPv6).") from exc


class Conversions(Resource):
    """The ``conversions`` module."""

    def __init__(self, transport: Transport, guard: Guard, campaigns: Campaigns):
        super().__init__(transport, guard)
        self._campaigns = campaigns

    def rules(self, account_id: int, include_shared: bool = False) -> list[ConversionRule]:
        """``GET /conversions?q=account&account={urn}``.

        ``include_shared`` adds ``conversionOwnershipTypes=List(OWNED,SHARED)``
        (202605+), which also returns rules shared across the Business Manager.
        """
        account_id = self._guard.check_account(account_id)
        query = [("q", "account"), ("account", restli.encode(restli.account_urn(account_id)))]
        if include_shared:
            query.append(("conversionOwnershipTypes", restli.restli_list(["OWNED", "SHARED"])))
        body = self._transport.get("/conversions", query)
        return [ConversionRule.from_api(raw) for raw in body.get("elements", [])]

    def rule(self, account_id: int, rule_id: int) -> ConversionRule:
        """``GET /conversions/{id}?account={urn}`` (``account`` is required)."""
        account_id = self._guard.check_account(account_id)
        body = self._transport.get(
            f"/conversions/{int(rule_id)}",
            (("account", restli.encode(restli.account_urn(account_id))),),
        )
        return ConversionRule.from_api(body)

    def create_rule(
        self,
        account_id: int,
        name: str,
        rule_type: str = "LEAD",
        post_click_window_days: int = 30,
        view_through_window_days: int = 7,
        confirm: bool = False,
        *,
        attribution_type: str = "LAST_TOUCH_BY_CAMPAIGN",
        auto_association: str | None = None,
    ) -> WriteResult:
        """Create one ``CONVERSIONS_API`` rule. Dry run by default.

        ``auto_association`` is ``ALL_CAMPAIGNS``, ``OBJECTIVE_BASED`` or None
        (no campaigns associated; the dry run warns).
        """
        action = "create_conversion_rule"
        account_id = self._guard.check_account(account_id)
        spec = ConversionRuleSpec(
            name, rule_type, post_click_window_days, view_through_window_days, attribution_type
        )
        body = spec.to_api(account_id, self._settings.api_version, action)
        query = _auto_association_query(auto_association, action)
        plan = WritePlan(
            action=action,
            request=Request("POST", "/conversions", query, json=body),
            target={"account_id": account_id, "name": body["name"]},
            summary={
                "rule_type": body["type"],
                "auto_association": dict(query).get("autoAssociationType", "NONE"),
            },
            warning="" if query else NO_ASSOCIATION_WARNING,
        )
        return self._guard.execute(plan, confirm)

    def create_rules(
        self,
        account_id: int,
        specs: Iterable[ConversionRuleSpec],
        confirm: bool = False,
        *,
        auto_association: str | None = None,
    ) -> WriteResult:
        """Create up to 100 rules in one ``BATCH_CREATE``. Dry run by default.

        ``auto_association`` applies to every rule that is created. Per-rule
        failures in LinkedIn's response are listed in ``warning``.
        """
        action = "create_conversion_rules"
        account_id = self._guard.check_account(account_id)
        specs = list(specs)
        if not specs or len(specs) > MAX_RULES_PER_BATCH:
            raise WriteRefusedError(
                "batch_too_large",
                action,
                f"create between 1 and {MAX_RULES_PER_BATCH} rules per call",
                count=len(specs),
            )
        elements = [spec.to_api(account_id, self._settings.api_version, action) for spec in specs]
        query = _auto_association_query(auto_association, action)
        plan = WritePlan(
            action=action,
            request=Request(
                "POST",
                "/conversions",
                query,
                json={"elements": elements},
                headers={"X-RestLi-Method": "BATCH_CREATE"},
            ),
            target={"account_id": account_id, "names": [element["name"] for element in elements]},
            summary={
                "rule_count": len(elements),
                "auto_association": dict(query).get("autoAssociationType", "NONE"),
            },
            warning="" if query else NO_ASSOCIATION_WARNING,
        )
        return _with_batch_failures(self._guard.execute(plan, confirm))

    def campaign_conversions(
        self, account_id: int, campaign_ids: Iterable[int], rule_id: int | None = None
    ) -> list[CampaignConversion]:
        """``GET /campaignConversions?q=campaigns&campaigns=List(...)``.

        The campaigns are first found under the account's own path, so an id
        from another account raises ``CampaignNotInAccountError`` before the
        association lookup. ``rule_id`` narrows the result to one rule.
        """
        account_id = self._guard.check_account(account_id)
        ids = _unique_ids(campaign_ids)
        if not ids:
            raise ValueError("Pass at least one campaign id.")
        missing = self._campaigns_outside(account_id, ids)
        if missing:
            raise CampaignNotInAccountError(account_id, missing)
        rows = self._associations(ids)
        if rule_id is None:
            return rows
        return [row for row in rows if row.rule_id == int(rule_id)]

    def associate_campaigns(
        self, account_id: int, rule_id: int, campaign_ids: Iterable[int], confirm: bool = False
    ) -> WriteResult:
        """Associate campaigns with a rule so they get conversions attributed. Dry run by default.

        Reads the rule (proving it is reachable under the account), checks every
        campaign belongs to the account, and reads the existing associations so
        only missing ones are sent. One campaign is a ``PUT`` on its key; several
        are one ``BATCH_UPDATE``. Associating again is harmless, and there is no
        way to remove an association here.
        """
        action = "associate_campaign_conversions"
        ids = _unique_ids(campaign_ids)
        if not ids or len(ids) > MAX_CAMPAIGNS_PER_ASSOCIATION:
            raise WriteRefusedError(
                "batch_too_large",
                action,
                f"associate between 1 and {MAX_CAMPAIGNS_PER_ASSOCIATION} campaigns per call",
                count=len(ids),
            )
        rule = self.rule(account_id, rule_id)
        missing = self._campaigns_outside(int(account_id), ids)
        if missing:
            raise WriteRefusedError(
                "not_in_account",
                action,
                f"campaign(s) {missing} are not in account {int(account_id)}",
                account_id=int(account_id),
                campaign_ids=missing,
            )
        existing = {row.campaign_id for row in self._associations(ids) if row.rule_id == rule.id}
        to_add = [i for i in ids if i not in existing]
        plan = WritePlan(
            action=action,
            request=_association_request(to_add or ids, rule.id),
            target={"account_id": int(account_id), "rule_id": rule.id, "rule_name": rule.name},
            before=sorted(existing),
            after=sorted(existing | set(ids)),
            summary={"campaign_count": len(to_add)},
            warning="" if rule.enabled else f"rule {rule.id} is disabled",
        )
        return _with_batch_failures(self._guard.execute(plan, confirm))

    def send_events(
        self,
        account_id: int,
        rule_id: int,
        events: Iterable[ConversionEvent],
        confirm: bool = False,
        now: datetime | None = None,
    ) -> WriteResult:
        """Stream conversion events to one rule (BATCH_CREATE). Dry run by default.

        The rule is read first to prove it belongs to the (allow-listed)
        account and is enabled; LinkedIn rejects events for disabled rules.
        """
        action = "send_conversion_events"
        now = now or datetime.now(UTC)
        events = list(events)
        if not events or len(events) > MAX_EVENTS_PER_BATCH:
            raise WriteRefusedError(
                "batch_too_large",
                action,
                f"send between 1 and {MAX_EVENTS_PER_BATCH} events per call",
                count=len(events),
            )
        rule = self.rule(account_id, rule_id)
        if not rule.enabled:
            raise WriteRefusedError(
                "rule_disabled", action, f"rule {rule.id} is disabled", rule_id=rule.id
            )
        api_version = self._settings.api_version
        try:
            elements = [event.to_api(rule.id, now, api_version) for event in events]
        except ValueError as exc:
            raise WriteRefusedError("invalid_input", action, str(exc)) from exc
        plan = WritePlan(
            action=action,
            request=Request(
                "POST",
                "/conversionEvents",
                json={"elements": elements},
                headers={"X-RestLi-Method": "BATCH_CREATE"},
            ),
            target={"account_id": int(account_id), "rule_id": rule.id, "rule_name": rule.name},
            summary={"event_count": len(elements)},
        )
        return self._guard.execute(plan, confirm)

    def _campaigns_outside(self, account_id: int, ids: list[int]) -> list[int]:
        """The ids a search under the account's own path does not return."""
        found = {
            campaign.id
            for campaign in self._campaigns.iter(account_id, ALL_CAMPAIGN_STATUSES, ids=ids)
        }
        return sorted(set(ids) - found)

    def _associations(self, campaign_ids: list[int]) -> list[CampaignConversion]:
        campaigns = restli.urn_list(restli.campaign_urn(i) for i in campaign_ids)
        request = Request(
            "GET", "/campaignConversions", (("q", "campaigns"), ("campaigns", campaigns))
        )
        return [
            CampaignConversion.from_api(raw)
            for raw in self._transport.iter_offset(request, ASSOCIATIONS_PAGE_SIZE)
        ]


def _association_body(campaign_id: int, rule_id: int) -> dict:
    return {"campaign": restli.campaign_urn(campaign_id), "conversion": conversion_urn(rule_id)}


def _association_request(campaign_ids: list[int], rule_id: int) -> Request:
    """A single ``PUT`` for one campaign, a ``BATCH_UPDATE`` for several."""
    if len(campaign_ids) == 1:
        campaign_id = campaign_ids[0]
        return Request(
            "PUT",
            f"/campaignConversions/{association_key(campaign_id, rule_id)}",
            json=_association_body(campaign_id, rule_id),
        )
    keys = [association_key(campaign_id, rule_id) for campaign_id in campaign_ids]
    entities = {
        key: _association_body(campaign_id, rule_id)
        for key, campaign_id in zip(keys, campaign_ids, strict=True)
    }
    return Request(
        "PUT",
        "/campaignConversions",
        (("ids", restli.restli_list(keys)),),
        json={"entities": entities},
        headers={"X-RestLi-Method": "BATCH_UPDATE"},
    )


def _with_batch_failures(result: WriteResult) -> WriteResult:
    """Surface per-element failures LinkedIn reports inside a 2xx batch response."""
    failed = batch_failures(result.response) if not result.dry_run else []
    if not failed:
        return result
    note = f"LinkedIn reported {len(failed)} failed element(s): {', '.join(failed)}"
    warning = f"{result.warning} {note}".strip()
    return replace(result, result="partially_applied", warning=warning)
