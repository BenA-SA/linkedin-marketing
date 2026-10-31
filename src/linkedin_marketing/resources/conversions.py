"""Conversions API: conversion rules and server-side conversion events. EXPERIMENTAL.

Needs the "Conversions API" product, scopes ``rw_conversions`` and ``r_ads``,
and an ad-account role above VIEWER. Built from the docs and LinkedIn's
official Postman collection ("Conversions API", requests "8a"/"8b"); not yet
verified against the live API.

Events are sent with ``X-RestLi-Method: BATCH_CREATE`` (up to 5,000 per
call; one invalid element fails the whole batch). ``conversionHappenedAt``
must be within the past 90 days. Emails are SHA-256 hashed locally; the
dry run and the audit log never contain a plain email, and the audit log
records only counts. ``event_id`` is LinkedIn's de-duplication key.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/conversions-api
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/conversion-tracking
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from linkedin_marketing import restli
from linkedin_marketing.errors import WriteRefusedError
from linkedin_marketing.hashing import hash_email
from linkedin_marketing.models import ConversionRule, to_money
from linkedin_marketing.resources.base import Resource, require_choice
from linkedin_marketing.safety import WritePlan, WriteResult
from linkedin_marketing.transport import Request

MAX_EVENTS_PER_BATCH = 5000
MAX_EVENT_AGE = timedelta(days=90)
RULE_TYPES = (
    "ADD_TO_CART",
    "APPLICATION_SUBMITTED",
    "BOOKED_APPOINTMENT",
    "COMPLETE_SIGNUP",
    "CONTACT",
    "DOWNLOAD",
    "INSTALL",
    "KEY_PAGE_VIEW",
    "LEAD",
    "MARKETING_QUALIFIED_LEAD",
    "OTHER",
    "OUTBOUND_CLICK",
    "PURCHASE",
    "QUALIFIED_LEAD",
    "REQUEST_QUOTE",
    "SALES_QUALIFIED_LEAD",
    "SAVE",
    "SEARCH",
    "SIGN_UP",
    "START_CHECKOUT",
    "SUBMIT_APPLICATION",
    "VIEW_CONTENT",
)


def conversion_urn(rule_id: int) -> str:
    return f"urn:lla:llaPartnerConversion:{int(rule_id)}"


@dataclass(frozen=True)
class ConversionEvent:
    """One offline/server-side conversion. Give at least one identifier.

    ``email`` may be plain (hashed here) or a SHA-256 hex digest.
    ``li_fat_id`` is LinkedIn's first-party click id from the landing URL.
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

    def to_api(self, rule_id: int, now: datetime) -> dict:
        """The wire shape, validated; raises ``ValueError`` on bad input."""
        happened = (
            self.happened_at if self.happened_at.tzinfo else self.happened_at.replace(tzinfo=UTC)
        )
        if happened > now or now - happened > MAX_EVENT_AGE:
            raise ValueError("happened_at must be within the past 90 days.")
        user_ids = []
        if self.email:
            user_ids.append({"idType": "SHA256_EMAIL", "idValue": hash_email(self.email)})
        if self.li_fat_id:
            user_ids.append(
                {"idType": "LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID", "idValue": self.li_fat_id}
            )
        user: dict = {"userIds": user_ids}
        user_info = self._user_info()
        if user_info:
            user["userInfo"] = user_info
        if not user_ids and not user_info:
            raise ValueError("Each event needs an email, li_fat_id, or first and last name.")
        event: dict = {
            "conversion": conversion_urn(rule_id),
            "conversionHappenedAt": int(happened.timestamp() * 1000),
            "user": user,
        }
        if self.value is not None:
            event["conversionValue"] = {
                "currencyCode": self.currency or "USD",
                "amount": str(to_money(Decimal(str(self.value)))),
            }
        if self.event_id:
            event["eventId"] = self.event_id
        return event

    def _user_info(self) -> dict:
        if not (self.first_name and self.last_name):
            return {}
        info = {"firstName": self.first_name, "lastName": self.last_name}
        optional = {
            "companyName": self.company,
            "title": self.title,
            "countryCode": self.country_code,
        }
        info.update({key: value for key, value in optional.items() if value})
        return info


class Conversions(Resource):
    """The ``conversions`` module."""

    def rules(self, account_id: int) -> list[ConversionRule]:
        """``GET /conversions?q=account&account={urn}``."""
        account_id = self._guard.check_account(account_id)
        body = self._transport.get(
            "/conversions",
            (("q", "account"), ("account", restli.encode(restli.account_urn(account_id)))),
        )
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
    ) -> WriteResult:
        """Create a ``CONVERSIONS_API`` rule (no campaigns associated). Dry run by default."""
        action = "create_conversion_rule"
        account_id = self._guard.check_account(account_id)
        rule_type = require_choice(rule_type, RULE_TYPES, action, "rule_type")
        body = {
            "name": name,
            "account": restli.account_urn(account_id),
            "conversionMethod": "CONVERSIONS_API",
            "type": rule_type,
            "postClickAttributionWindowSize": int(post_click_window_days),
            "viewThroughAttributionWindowSize": int(view_through_window_days),
        }
        plan = WritePlan(
            action=action,
            request=Request("POST", "/conversions", json=body),
            target={"account_id": account_id, "name": name},
            summary={"rule_type": rule_type},
        )
        return self._guard.execute(plan, confirm)

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
        try:
            elements = [event.to_api(rule.id, now) for event in events]
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
