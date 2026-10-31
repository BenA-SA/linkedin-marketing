"""Advertising API: accounts, campaign groups, campaigns, creatives, targeting,
previews and forecasting.

Campaign group, campaign and creative paths are nested under
``/adAccounts/{id}/`` (since 202305), so LinkedIn also scopes them to that
account server-side, on top of the local allow-list check.

Writes are limited on purpose: pausing/resuming a campaign or campaign group
and changing a campaign's daily budget. Creating campaigns or creatives is
excluded (a mistake there spends money on a live audience); build those in
Campaign Manager. Nothing here deletes.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/ads/account-structure/create-and-manage-accounts
https://learn.microsoft.com/linkedin/marketing/integrations/ads/account-structure/create-and-manage-account-users
https://learn.microsoft.com/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups
https://learn.microsoft.com/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaigns
https://learn.microsoft.com/linkedin/marketing/integrations/ads/account-structure/create-and-manage-creatives
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation

from linkedin_marketing import restli
from linkedin_marketing.errors import WriteRefusedError
from linkedin_marketing.models import (
    AccountUser,
    AdAccount,
    Campaign,
    CampaignGroup,
    Creative,
    to_money,
)
from linkedin_marketing.resources.base import Resource, require_choice, take, upper_all
from linkedin_marketing.resources.forecasting import Forecasting
from linkedin_marketing.resources.targeting import Previews, Targeting
from linkedin_marketing.safety import Guard, WritePlan, WriteResult
from linkedin_marketing.transport import Request, Transport

SEARCH_PAGE_SIZE = 100
CREATIVE_PAGE_SIZE = 100
DEFAULT_ACCOUNT_STATUSES = ("ACTIVE", "DRAFT")
DEFAULT_CAMPAIGN_GROUP_STATUSES = ("ACTIVE", "PAUSED", "DRAFT")
DEFAULT_CAMPAIGN_STATUSES = ("ACTIVE", "PAUSED", "DRAFT", "COMPLETED")
DEFAULT_CREATIVE_STATUSES = ("ACTIVE", "PAUSED", "DRAFT")
SWITCHABLE_STATUSES = ("ACTIVE", "PAUSED")
_PARTIAL_UPDATE = {"X-RestLi-Method": "PARTIAL_UPDATE"}


def _partial_update(path: str, changes: dict) -> Request:
    return Request("POST", path, json={"patch": {"$set": changes}}, headers=_PARTIAL_UPDATE)


def _parse_budget(amount: object, action: str) -> Decimal:
    """``amount`` as finite 2dp money, or a structured refusal."""
    try:
        value = to_money(Decimal(str(amount)))
    except InvalidOperation as exc:
        raise WriteRefusedError(
            "budget_not_positive", action, f"{amount!r} is not a usable amount"
        ) from exc
    if not value.is_finite() or value <= 0:
        raise WriteRefusedError("budget_not_positive", action, f"{amount!r} is not above zero")
    return value


class Accounts(Resource):
    """Ad accounts the authenticated member can access."""

    def iter(self, statuses: Iterable[str] = DEFAULT_ACCOUNT_STATUSES) -> Iterator[AdAccount]:
        """Accounts with the given statuses; only allow-listed ones when a list is set.

        ``GET /adAccounts?q=search`` with a ``pageSize``/``pageToken`` cursor.
        """
        request = Request(
            "GET",
            "/adAccounts",
            (
                ("q", "search"),
                ("search", restli.search_criteria(status=upper_all(statuses))),
                ("sortOrder", "DESCENDING"),
            ),
        )
        allowed = self._settings.allowed_ad_account_ids
        for raw in self._transport.iter_cursor(request, SEARCH_PAGE_SIZE):
            if not allowed or int(raw.get("id", 0)) in allowed:
                yield AdAccount.from_api(raw)

    def list(
        self, statuses: Iterable[str] = DEFAULT_ACCOUNT_STATUSES, limit: int | None = None
    ) -> list[AdAccount]:
        return take(self.iter(statuses), limit)

    def get(self, account_id: int) -> AdAccount:
        """``GET /adAccounts/{id}``."""
        account_id = self._guard.check_account(account_id)
        return AdAccount.from_api(self._transport.get(f"/adAccounts/{account_id}"))

    def users(self, account_id: int, limit: int | None = None) -> list[AccountUser]:
        """Members with a role on the account: ``GET /adAccountUsers?q=accounts``."""
        account_id = self._guard.check_account(account_id)
        request = Request(
            "GET",
            "/adAccountUsers",
            (("q", "accounts"), ("accounts", restli.encode(restli.account_urn(account_id)))),
        )
        rows = self._transport.iter_offset(request, SEARCH_PAGE_SIZE)
        return take((AccountUser.from_api(raw) for raw in rows), limit)


class CampaignGroups(Resource):
    """Campaign groups. Their search rejects ``COMPLETED``, unlike campaigns."""

    def _path(self, account_id: int) -> str:
        return f"/adAccounts/{self._guard.check_account(account_id)}/adCampaignGroups"

    def iter(
        self,
        account_id: int,
        statuses: Iterable[str] = DEFAULT_CAMPAIGN_GROUP_STATUSES,
        ids: Iterable[int] = (),
    ) -> Iterator[CampaignGroup]:
        """``GET .../adCampaignGroups?q=search`` (cursor paging)."""
        group_urns = [restli.encode(restli.campaign_group_urn(int(i))) for i in ids]
        criteria = restli.search_criteria(status=upper_all(statuses), id=group_urns)
        request = Request(
            "GET",
            self._path(account_id),
            (("q", "search"), ("search", criteria), ("sortOrder", "DESCENDING")),
        )
        for raw in self._transport.iter_cursor(request, SEARCH_PAGE_SIZE):
            yield CampaignGroup.from_api(raw)

    def list(
        self,
        account_id: int,
        statuses: Iterable[str] = DEFAULT_CAMPAIGN_GROUP_STATUSES,
        limit: int | None = None,
    ) -> list[CampaignGroup]:
        return take(self.iter(account_id, statuses), limit)

    def get(self, account_id: int, group_id: int) -> CampaignGroup:
        return CampaignGroup.from_api(
            self._transport.get(f"{self._path(account_id)}/{int(group_id)}")
        )

    def set_status(
        self, account_id: int, group_id: int, status: str, confirm: bool = False
    ) -> WriteResult:
        """Pause or resume a campaign group (ACTIVE <-> PAUSED only). Dry run by default."""
        action = "set_campaign_group_status"
        status = require_choice(status, SWITCHABLE_STATUSES, action, "status")
        group = self.get(account_id, group_id)
        _require_switchable(group.status, action, group_id=group.id)
        plan = WritePlan(
            action=action,
            request=_partial_update(f"{self._path(account_id)}/{group.id}", {"status": status}),
            target={
                "account_id": int(account_id),
                "campaign_group_id": group.id,
                "name": group.name,
            },
            before=group.status,
            after=status,
        )
        return self._guard.execute(plan, confirm)


def _require_switchable(current: str, action: str, **target) -> None:
    if current not in SWITCHABLE_STATUSES:
        raise WriteRefusedError(
            "current_status_not_switchable",
            action,
            f"it is {current}; only ACTIVE <-> PAUSED is allowed here",
            current_status=current,
            **target,
        )


class Campaigns(Resource):
    """Campaigns (Campaign Manager's "ad sets")."""

    def _path(self, account_id: int) -> str:
        return f"/adAccounts/{self._guard.check_account(account_id)}/adCampaigns"

    def iter(
        self,
        account_id: int,
        statuses: Iterable[str] = DEFAULT_CAMPAIGN_STATUSES,
        campaign_group_id: int | None = None,
        ids: Iterable[int] = (),
    ) -> Iterator[Campaign]:
        """``GET .../adCampaigns?q=search`` (cursor paging)."""
        groups = (
            [restli.encode(restli.campaign_group_urn(int(campaign_group_id)))]
            if campaign_group_id
            else []
        )
        campaign_urns = [restli.encode(restli.campaign_urn(int(i))) for i in ids]
        criteria = restli.search_criteria(
            status=upper_all(statuses), campaignGroup=groups, id=campaign_urns
        )
        request = Request(
            "GET",
            self._path(account_id),
            (("q", "search"), ("search", criteria), ("sortOrder", "DESCENDING")),
        )
        for raw in self._transport.iter_cursor(request, SEARCH_PAGE_SIZE):
            yield Campaign.from_api(raw)

    def list(
        self,
        account_id: int,
        statuses: Iterable[str] = DEFAULT_CAMPAIGN_STATUSES,
        campaign_group_id: int | None = None,
        limit: int | None = None,
    ) -> list[Campaign]:
        return take(self.iter(account_id, statuses, campaign_group_id), limit)

    def get(self, account_id: int, campaign_id: int) -> Campaign:
        return Campaign.from_api(
            self._transport.get(f"{self._path(account_id)}/{int(campaign_id)}")
        )

    def _owned(self, account_id: int, campaign_id: int, action: str) -> Campaign:
        campaign = self.get(account_id, campaign_id)
        if campaign.account_id and campaign.account_id != str(int(account_id)):
            raise WriteRefusedError(
                "not_in_account",
                action,
                f"campaign {campaign_id} belongs to account {campaign.account_id}",
                account_id=int(account_id),
                campaign_id=int(campaign_id),
            )
        return campaign

    def set_status(
        self, account_id: int, campaign_id: int, status: str, confirm: bool = False
    ) -> WriteResult:
        """Pause or resume one campaign (ACTIVE <-> PAUSED only). Dry run by default.

        DRAFT, ARCHIVED and COMPLETED campaigns are refused, so nothing can be
        launched or revived from here.
        """
        action = "set_campaign_status"
        status = require_choice(status, SWITCHABLE_STATUSES, action, "status")
        campaign = self._owned(account_id, campaign_id, action)
        _require_switchable(campaign.status, action, campaign_id=campaign.id)
        plan = WritePlan(
            action=action,
            request=_partial_update(f"{self._path(account_id)}/{campaign.id}", {"status": status}),
            target={
                "account_id": int(account_id),
                "campaign_id": campaign.id,
                "name": campaign.name,
            },
            before=campaign.status,
            after=status,
        )
        return self._guard.execute(plan, confirm)

    def set_daily_budget(
        self, account_id: int, campaign_id: int, amount: str | Decimal, confirm: bool = False
    ) -> WriteResult:
        """Change one campaign's daily budget. Dry run by default.

        The amount is in the campaign's existing currency, must be above zero
        and at most ``LINKEDIN_MAX_DAILY_BUDGET``; with no cap configured,
        budget changes are refused.
        """
        action = "set_campaign_daily_budget"
        new_amount = _parse_budget(amount, action)
        self._check_cap(new_amount, action, campaign_id)
        campaign = self._owned(account_id, campaign_id, action)
        if campaign.daily_budget is None:
            raise WriteRefusedError(
                "no_daily_budget",
                action,
                "the campaign has no daily budget to change",
                campaign_id=campaign.id,
            )
        currency = campaign.daily_budget.currency
        changes = {"dailyBudget": {"amount": str(new_amount), "currencyCode": currency}}
        plan = WritePlan(
            action=action,
            request=_partial_update(f"{self._path(account_id)}/{campaign.id}", changes),
            target={
                "account_id": int(account_id),
                "campaign_id": campaign.id,
                "name": campaign.name,
            },
            before={"amount": str(to_money(campaign.daily_budget.amount)), "currency": currency},
            after={"amount": str(new_amount), "currency": currency},
        )
        return self._guard.execute(plan, confirm)

    def _check_cap(self, amount: Decimal, action: str, campaign_id: int) -> None:
        cap = self._settings.max_daily_budget
        if cap is None:
            raise WriteRefusedError(
                "no_budget_cap",
                action,
                "set LINKEDIN_MAX_DAILY_BUDGET to allow daily-budget changes",
                campaign_id=int(campaign_id),
            )
        if amount > cap:
            raise WriteRefusedError(
                "budget_over_cap",
                action,
                f"{amount} exceeds LINKEDIN_MAX_DAILY_BUDGET={cap}",
                campaign_id=int(campaign_id),
                requested=str(amount),
                cap=str(cap),
            )


class Creatives(Resource):
    """Creatives (ads). Read-only: creating ads is excluded on purpose."""

    def _path(self, account_id: int) -> str:
        return f"/adAccounts/{self._guard.check_account(account_id)}/creatives"

    def iter(
        self,
        account_id: int,
        campaign_ids: Iterable[int] = (),
        statuses: Iterable[str] = DEFAULT_CREATIVE_STATUSES,
    ) -> Iterator[Creative]:
        """``GET .../creatives?q=criteria`` with ``X-RestLi-Method: FINDER``."""
        query = [("q", "criteria"), ("sortOrder", "DESCENDING")]
        campaign_urns = [restli.campaign_urn(int(cid)) for cid in campaign_ids]
        if campaign_urns:
            query.append(("campaigns", restli.urn_list(campaign_urns)))
        status_values = upper_all(statuses)
        if status_values:
            query.append(("intendedStatuses", restli.restli_list(status_values)))
        request = Request(
            "GET", self._path(account_id), tuple(query), headers={"X-RestLi-Method": "FINDER"}
        )
        for raw in self._transport.iter_cursor(request, CREATIVE_PAGE_SIZE):
            yield Creative.from_api(raw)

    def list(
        self,
        account_id: int,
        campaign_ids: Iterable[int] = (),
        statuses: Iterable[str] = DEFAULT_CREATIVE_STATUSES,
        limit: int | None = None,
    ) -> list[Creative]:
        return take(self.iter(account_id, campaign_ids, statuses), limit)

    def get(self, account_id: int, creative_id: int) -> Creative:
        """``GET .../creatives/{encoded creative URN}``."""
        key = restli.encode(restli.creative_urn(int(creative_id)))
        return Creative.from_api(self._transport.get(f"{self._path(account_id)}/{key}"))


class Ads:
    """The ``ads`` module: everything under the Advertising API product."""

    def __init__(self, transport: Transport, guard: Guard):
        self.accounts = Accounts(transport, guard)
        self.campaign_groups = CampaignGroups(transport, guard)
        self.campaigns = Campaigns(transport, guard)
        self.creatives = Creatives(transport, guard)
        self.targeting = Targeting(transport, guard)
        self.previews = Previews(transport, guard)
        self.forecasting = Forecasting(transport, guard)
