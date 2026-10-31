"""Ad analytics (``/adAnalytics``) with derived CTR, CPC, CPM and CPL.

Scoping: every report is faceted by one allow-listed ad account. Narrowing
to campaigns or campaign groups first confirms, with one search under that
account's path, that each id belongs to it, then facets on those ids. So a
caller can narrow a report but never widen it beyond the allow-list.

Money (``costInLocalCurrency``, in the account currency) stays ``Decimal``
and is quantised to 2dp on output. Ratios are ``None`` when the
denominator is zero rather than a misleading 0.

Demographic (``MEMBER_*``) pivots carry LinkedIn's privacy rules: values
are approximate, values with fewer than 3 events are dropped, only the top
100 values per creative per day are returned, data lags 12-24 hours, and
demographic data is kept for 2 years. ``approximateMemberReach`` and
``conversionValueInLocalCurrency`` are unavailable with those pivots. LinkedIn
also adds noise to small daily counts, so a total over the range
(``timeGranularity=ALL``) beats a sum of DAILY rows. A response holds at most
15,000 rows and is not paged.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/ads-reporting
https://learn.microsoft.com/linkedin/marketing/integrations/ads-reporting/ads-reporting-schema
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from linkedin_marketing import restli
from linkedin_marketing.errors import LinkedInError
from linkedin_marketing.models import parse_decimal, to_money
from linkedin_marketing.resources.base import Resource, upper_all
from linkedin_marketing.resources.targeting import Targeting

MAX_METRIC_FIELDS = 20
MAX_STATISTICS_PIVOTS = 3
DEFAULT_METRICS = (
    "impressions",
    "clicks",
    "landingPageClicks",
    "costInLocalCurrency",
    "oneClickLeads",
    "oneClickLeadFormOpens",
    "externalWebsiteConversions",
)
MONEY_METRICS = frozenset({"costInLocalCurrency", "costInUsd", "conversionValueInLocalCurrency"})
DEMOGRAPHIC_UNAVAILABLE = frozenset(
    {
        "approximateMemberReach",
        "conversionValueInLocalCurrency",
        "cardImpressions",
        "cardClicks",
        "viralCardImpressions",
        "viralCardClicks",
    }
)
TIME_GRANULARITIES = ("ALL", "DAILY", "MONTHLY", "YEARLY")
DEMOGRAPHIC_PRIVACY_NOTE = (
    "Demographic values are approximate; values under 3 events are dropped; "
    "top 100 values per creative per day; 12-24h delay; 2-year retention."
)
_PERCENT = Decimal("0.01")


def _ratio_money(numerator: Decimal, denominator: int, scale: int = 1) -> str | None:
    if not denominator:
        return None
    return str(to_money(numerator * scale / denominator))


def _percent(part: int | Decimal, whole: int | Decimal) -> str | None:
    if not whole:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(_PERCENT, ROUND_HALF_UP))


def derived_metrics(metrics: dict) -> dict:
    """CTR %, CPC, CPM, CPL, cost per conversion and lead-form completion %."""
    spend = metrics.get("costInLocalCurrency") or Decimal(0)
    impressions = int(metrics.get("impressions") or 0)
    clicks = int(metrics.get("clicks") or 0)
    leads = int(metrics.get("oneClickLeads") or 0)
    opens = int(metrics.get("oneClickLeadFormOpens") or 0)
    conversions = int(metrics.get("externalWebsiteConversions") or 0)
    return {
        "ctr_percent": _percent(clicks, impressions),
        "cpc": _ratio_money(spend, clicks),
        "cpm": _ratio_money(spend, impressions, 1000),
        "cpl": _ratio_money(spend, leads),
        "cost_per_conversion": _ratio_money(spend, conversions),
        "lead_form_completion_percent": _percent(leads, opens),
    }


def _day(raw: dict | None) -> date | None:
    if not raw:
        return None
    return date(raw["year"], raw["month"], raw["day"])


def _metric_value(name: str, value: object) -> object:
    if name in MONEY_METRICS:
        return parse_decimal(value) or Decimal(0)
    return value


@dataclass(frozen=True)
class AnalyticsRow:
    """One combination of pivot values over one time bucket."""

    pivot_values: list[str]
    start: date | None
    end: date | None
    metrics: dict
    names: list[str | None] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict, metric_names: Iterable[str]) -> AnalyticsRow:
        date_range = raw.get("dateRange") or {}
        return cls(
            pivot_values=list(raw.get("pivotValues") or []),
            start=_day(date_range.get("start")),
            end=_day(date_range.get("end")),
            metrics={name: _metric_value(name, raw.get(name, 0)) for name in metric_names},
        )

    @property
    def pivot_id(self) -> str:
        return restli.urn_id(self.pivot_values[0]) if self.pivot_values else ""

    def to_dict(self, currency: str) -> dict:
        """One flat, JSON-safe record (good for ``pandas.DataFrame``)."""
        record: dict = {
            "pivot_values": self.pivot_values,
            "pivot_id": self.pivot_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "currency": currency,
        }
        if self.names:
            record["pivot_names"] = self.names
        for name, value in self.metrics.items():
            record[name] = str(to_money(value)) if isinstance(value, Decimal) else value
        record.update(derived_metrics(self.metrics))
        return record


@dataclass(frozen=True)
class AnalyticsReport:
    """Rows plus totals; ``to_records()`` is DataFrame-ready."""

    account_id: int
    pivots: list[str]
    time_granularity: str
    start: date
    end: date | None
    currency: str
    rows: list[AnalyticsRow]
    notes: list[str] = field(default_factory=list)

    def totals(self) -> dict:
        """Summed metrics over all rows, with derived ratios."""
        summed: dict = {}
        for row in self.rows:
            for name, value in row.metrics.items():
                if isinstance(value, int | Decimal):
                    summed[name] = summed.get(name, 0) + value
        record = {
            name: str(to_money(value)) if isinstance(value, Decimal) else value
            for name, value in summed.items()
        }
        return {"currency": self.currency, **record, **derived_metrics(summed)}

    def to_records(self) -> list[dict]:
        return [row.to_dict(self.currency) for row in self.rows]

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "pivots": self.pivots,
            "time_granularity": self.time_granularity,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "currency": self.currency,
            "rows": self.to_records(),
            "totals": self.totals(),
            "notes": self.notes,
        }


def _metric_fields(metrics: Iterable[str] | None) -> list[str]:
    names = list(dict.fromkeys(metrics or DEFAULT_METRICS))
    if len(names) > MAX_METRIC_FIELDS:
        raise ValueError(f"LinkedIn allows at most {MAX_METRIC_FIELDS} metrics per call.")
    return names


def _is_demographic(pivots: list[str]) -> bool:
    return any(pivot.startswith("MEMBER_") for pivot in pivots)


class Reporting(Resource):
    """The ``reporting`` module: ``adAnalytics`` finders."""

    def __init__(self, transport, guard, campaigns, campaign_groups, accounts):
        super().__init__(transport, guard)
        self._campaigns = campaigns
        self._campaign_groups = campaign_groups
        self._accounts = accounts
        self._targeting = Targeting(transport, guard)

    def analytics(
        self,
        account_id: int,
        start: date,
        end: date | None = None,
        pivot: str = "CAMPAIGN",
        time_granularity: str = "ALL",
        metrics: Iterable[str] | None = None,
        campaign_ids: Iterable[int] = (),
        campaign_group_ids: Iterable[int] = (),
    ) -> AnalyticsReport:
        """One-pivot report: ``GET /adAnalytics?q=analytics``.

        ``start``/``end`` are inclusive UTC dates. ``metrics`` defaults to
        spend, impressions, clicks, landing-page clicks, leads, lead-form
        opens and website conversions (max 20).
        """
        return self._report(
            "analytics",
            [pivot],
            account_id,
            start,
            end,
            time_granularity,
            metrics,
            campaign_ids,
            campaign_group_ids,
        )

    def statistics(
        self,
        account_id: int,
        start: date,
        pivots: Iterable[str],
        end: date | None = None,
        time_granularity: str = "ALL",
        metrics: Iterable[str] | None = None,
        campaign_ids: Iterable[int] = (),
        campaign_group_ids: Iterable[int] = (),
    ) -> AnalyticsReport:
        """Up to three pivots at once: ``GET /adAnalytics?q=statistics``."""
        pivots = upper_all(pivots)
        if not 1 <= len(pivots) <= MAX_STATISTICS_PIVOTS:
            raise ValueError("statistics needs one to three pivots.")
        return self._report(
            "statistics",
            pivots,
            account_id,
            start,
            end,
            time_granularity,
            metrics,
            campaign_ids,
            campaign_group_ids,
        )

    def demographics(
        self,
        account_id: int,
        start: date,
        pivot: str = "MEMBER_JOB_FUNCTION",
        end: date | None = None,
        metrics: Iterable[str] | None = None,
        campaign_ids: Iterable[int] = (),
        resolve_names: bool = True,
    ) -> AnalyticsReport:
        """Who saw and clicked, by a ``MEMBER_*`` pivot, with readable names.

        Metrics LinkedIn withholds for demographic pivots are dropped. Name
        lookup failure does not hide the data: rows keep their URNs and a
        note says why names are missing.
        """
        pivot = pivot.upper()
        if not pivot.startswith("MEMBER_"):
            raise ValueError("demographics needs a MEMBER_* pivot, e.g. MEMBER_JOB_FUNCTION.")
        wanted = [name for name in _metric_fields(metrics) if name not in DEMOGRAPHIC_UNAVAILABLE]
        report = self.analytics(account_id, start, end, pivot, "ALL", wanted, campaign_ids)
        if not resolve_names:
            return report
        return self._with_names(report)

    def _with_names(self, report: AnalyticsReport) -> AnalyticsReport:
        urns = [row.pivot_values[0] for row in report.rows if row.pivot_values]
        try:
            names = {entity.urn: entity.name for entity in self._targeting.resolve(urns)}
        except LinkedInError as exc:
            note = f"Pivot names unavailable ({type(exc).__name__}); rows show URNs."
            return AnalyticsReport(**{**report.__dict__, "notes": [*report.notes, note]})
        rows = [
            AnalyticsRow(
                row.pivot_values,
                row.start,
                row.end,
                row.metrics,
                [names.get(value) for value in row.pivot_values],
            )
            for row in report.rows
        ]
        return AnalyticsReport(**{**report.__dict__, "rows": rows})

    def _facet(self, account_id: int, campaign_ids, campaign_group_ids) -> tuple[str, str]:
        """The facet parameter: the account, or ids verified to belong to it."""
        campaign_ids = [int(i) for i in campaign_ids]
        group_ids = [int(i) for i in campaign_group_ids]
        if campaign_ids:
            found = {
                c.id
                for c in self._campaigns.iter(account_id, _ALL_CAMPAIGN_STATUSES, ids=campaign_ids)
            }
            self._require_all(campaign_ids, found, "campaign", account_id)
            return "campaigns", restli.urn_list(restli.campaign_urn(i) for i in campaign_ids)
        if group_ids:
            found = {
                g.id
                for g in self._campaign_groups.iter(account_id, _ALL_GROUP_STATUSES, ids=group_ids)
            }
            self._require_all(group_ids, found, "campaign group", account_id)
            return "campaignGroups", restli.urn_list(
                restli.campaign_group_urn(i) for i in group_ids
            )
        return "accounts", restli.urn_list([restli.account_urn(account_id)])

    @staticmethod
    def _require_all(wanted: list[int], found: set[int], kind: str, account_id: int) -> None:
        missing = sorted(set(wanted) - found)
        if missing:
            raise ValueError(
                f"{kind} id(s) {missing} are not in ad account {account_id}; "
                "reports cannot reach outside the account."
            )

    def _report(
        self,
        finder,
        pivots,
        account_id,
        start,
        end,
        time_granularity,
        metrics,
        campaign_ids,
        campaign_group_ids,
    ) -> AnalyticsReport:
        account_id = self._guard.check_account(account_id)
        pivots = upper_all(pivots)
        granularity = time_granularity.upper()
        if granularity not in TIME_GRANULARITIES:
            raise ValueError(f"time_granularity must be one of {', '.join(TIME_GRANULARITIES)}.")
        names = _metric_fields(metrics)
        facet = self._facet(account_id, campaign_ids, campaign_group_ids)
        pivot_param = (
            ("pivot", pivots[0])
            if finder == "analytics"
            else ("pivots", restli.restli_list(pivots))
        )
        query = (
            ("q", finder),
            pivot_param,
            ("timeGranularity", granularity),
            ("dateRange", restli.date_range(start, end)),
            facet,
            ("fields", ",".join(["dateRange", "pivotValues", *names])),
        )
        body = self._transport.get("/adAnalytics", query)
        currency = self._accounts.get(account_id).currency
        rows = [AnalyticsRow.from_api(raw, names) for raw in body.get("elements", [])]
        rows.sort(key=lambda row: (row.start or date.min, row.pivot_values))
        notes = [DEMOGRAPHIC_PRIVACY_NOTE] if _is_demographic(pivots) else []
        return AnalyticsReport(account_id, pivots, granularity, start, end, currency, rows, notes)


_ALL_CAMPAIGN_STATUSES = ("ACTIVE", "PAUSED", "DRAFT", "COMPLETED", "ARCHIVED", "CANCELED")
_ALL_GROUP_STATUSES = ("ACTIVE", "PAUSED", "DRAFT", "ARCHIVED", "CANCELED")
