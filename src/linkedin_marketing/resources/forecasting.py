"""Bid and budget guidance and delivery forecasts for a would-be campaign.

Both finders are reads: they describe a campaign that does not exist yet.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/ad-budget-pricing
https://learn.microsoft.com/linkedin/marketing/integrations/ads/advertising-targeting/ad-supply-forecasts
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from linkedin_marketing import restli
from linkedin_marketing.models import Money
from linkedin_marketing.resources.base import Resource


def _money(amount: Decimal | str, currency: str) -> str:
    return f"(amount:{restli.encode(str(amount))},currencyCode:{currency})"


def _money_block(raw: dict | None) -> dict:
    """``{default,min,max}`` of Money objects → strings."""
    raw = raw or {}
    out = {}
    for key, value in raw.items():
        money = Money.from_api(value)
        out[key] = money.to_dict() if money else None
    return out


class Forecasting(Resource):
    """``adBudgetPricing`` and ``adSupplyForecasts``."""

    def budget_pricing(
        self,
        account_id: int,
        targeting_criteria: dict,
        currency: str,
        bid_type: str = "CPM",
        campaign_type: str = "SPONSORED_UPDATES",
        match_type: str = "EXACT",
        daily_budget: Decimal | str | None = None,
        objective_type: str | None = None,
    ) -> dict:
        """Suggested bid, bid limits and daily-budget limits: ``q=criteriaV2``."""
        account_id = self._guard.check_account(account_id)
        params = [
            ("q", "criteriaV2"),
            ("account", restli.encode(restli.account_urn(account_id))),
            ("campaignType", campaign_type),
            ("bidType", bid_type),
            ("matchType", match_type),
            ("currency", currency),
            ("targetingCriteria", restli.serialize(targeting_criteria)),
        ]
        if daily_budget is not None:
            params.append(("dailyBudget", _money(daily_budget, currency)))
        if objective_type:
            params.append(("objectiveType", objective_type))
        body = self._transport.get("/adBudgetPricing", params)
        first = next(iter(body.get("elements", [])), {})
        return {
            "suggested_bid": _money_block(first.get("suggestedBid")),
            "bid_limits": _money_block(first.get("bidLimits")),
            "daily_budget_limits": _money_block(first.get("dailyBudgetLimits")),
        }

    def supply_forecast(
        self,
        account_id: int,
        targeting_criteria: dict,
        start: date,
        end: date,
        daily_budget: Decimal | str,
        currency: str,
        campaign_type: str = "SPONSORED_UPDATES",
        objective_type: str | None = None,
    ) -> list[dict]:
        """Forecast impressions, clicks, spend, leads: ``adSupplyForecasts?q=criteriaV2``.

        The time range must start in the future.
        """
        account_id = self._guard.check_account(account_id)
        params = [
            ("q", "criteriaV2"),
            ("account", restli.encode(restli.account_urn(account_id))),
            ("campaignType", campaign_type),
            ("timeRange", restli.time_range_ms(restli.epoch_ms(start), restli.epoch_ms(end))),
            ("targetingCriteria", restli.serialize(targeting_criteria)),
            ("dailyBudget", _money(daily_budget, currency)),
        ]
        if objective_type:
            params.append(("objectiveType", objective_type))
        body = self._transport.get("/adSupplyForecasts", params)
        return [
            {
                "metric": raw.get("metricType"),
                "granularity": raw.get("granularity"),
                "series": raw.get("timeSeries", []),
            }
            for raw in body.get("elements", [])
        ]
