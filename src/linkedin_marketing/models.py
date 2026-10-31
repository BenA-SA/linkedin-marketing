"""Typed, flattened views of LinkedIn entities, each keeping the raw payload.

Every model is a frozen dataclass built with ``from_api(raw)``. The fields
are the ones people actually read; ``raw`` keeps the full API element for
anything else. ``to_dict()`` drops ``raw`` and gives JSON-safe values (money
as 2dp strings, times as ISO 8601), which is what the MCP tools return and
what ``pandas.DataFrame([...])`` wants.

Money stays ``Decimal`` in the objects and is quantised to 2dp with
``ROUND_HALF_UP`` on the way out, so a float never touches a budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from linkedin_marketing.restli import urn_id

_MONEY = Decimal("0.01")
_FACET_PREFIX = "urn:li:adTargetingFacet:"
_SAMPLE_VALUES = 5


def to_money(value: Decimal) -> Decimal:
    """``value`` quantised to 2dp, half-up."""
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def parse_decimal(value: object) -> Decimal | None:
    """A ``Decimal`` from LinkedIn's string/number amounts, or None."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def epoch_ms_to_iso(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(to_money(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Money):
        return value.to_dict()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


class Model:
    """Shared ``to_dict``: every field except ``raw``, JSON-safe."""

    def to_dict(self) -> dict:
        return {
            item.name: _jsonable(getattr(self, item.name))
            for item in fields(self)  # type: ignore[arg-type]
            if item.name != "raw"
        }


@dataclass(frozen=True)
class Money(Model):
    """An amount in a currency (``{amount, currencyCode}`` on the wire)."""

    amount: Decimal
    currency: str

    @classmethod
    def from_api(cls, raw: dict | None) -> Money | None:
        if not raw:
            return None
        amount = parse_decimal(raw.get("amount"))
        if amount is None:
            return None
        return cls(amount, raw.get("currencyCode", ""))

    def to_dict(self) -> dict:
        return {"amount": str(to_money(self.amount)), "currency": self.currency}


def _facets(facets: dict) -> list[dict]:
    return [
        {
            "facet": name.removeprefix(_FACET_PREFIX),
            "count": len(values),
            "sample": list(values)[:_SAMPLE_VALUES],
        }
        for name, values in facets.items()
    ]


def targeting_summary(criteria: dict | None) -> dict:
    """Included facets (AND of ORs) and excluded facets, values sampled."""
    criteria = criteria or {}
    include: list[dict] = []
    for clause in (criteria.get("include") or {}).get("and") or []:
        include.extend(_facets(clause.get("or") or {}))
    exclude = _facets(((criteria.get("exclude") or {}).get("or")) or {})
    return {"include": include, "exclude": exclude}


@dataclass(frozen=True)
class AdAccount(Model):
    id: int
    name: str
    status: str
    type: str
    currency: str
    test: bool
    serving_statuses: list[str]
    reference: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> AdAccount:
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name", ""),
            status=raw.get("status", ""),
            type=raw.get("type", ""),
            currency=raw.get("currency", ""),
            test=bool(raw.get("test", False)),
            serving_statuses=list(raw.get("servingStatuses") or []),
            reference=raw.get("reference"),
            raw=raw,
        )


@dataclass(frozen=True)
class AccountUser(Model):
    account_id: str
    user: str
    role: str
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> AccountUser:
        return cls(urn_id(raw.get("account")), raw.get("user", ""), raw.get("role", ""), raw)


@dataclass(frozen=True)
class CampaignGroup(Model):
    id: int
    name: str
    status: str
    serving_statuses: list[str]
    total_budget: Money | None
    daily_budget: Money | None
    start: str | None
    end: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> CampaignGroup:
        schedule = raw.get("runSchedule") or {}
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name", ""),
            status=raw.get("status", ""),
            serving_statuses=list(raw.get("servingStatuses") or []),
            total_budget=Money.from_api(raw.get("totalBudget")),
            daily_budget=Money.from_api(raw.get("dailyBudget")),
            start=epoch_ms_to_iso(schedule.get("start")),
            end=epoch_ms_to_iso(schedule.get("end")),
            raw=raw,
        )


@dataclass(frozen=True)
class Campaign(Model):
    """A campaign (Campaign Manager's "ad set")."""

    id: int
    name: str
    status: str
    serving_statuses: list[str]
    campaign_group_id: str
    account_id: str
    objective: str | None
    type: str | None
    cost_type: str | None
    daily_budget: Money | None
    total_budget: Money | None
    unit_cost: Money | None
    start: str | None
    end: str | None
    targeting: dict
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Campaign:
        schedule = raw.get("runSchedule") or {}
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name", ""),
            status=raw.get("status", ""),
            serving_statuses=list(raw.get("servingStatuses") or []),
            campaign_group_id=urn_id(raw.get("campaignGroup")),
            account_id=urn_id(raw.get("account")),
            objective=raw.get("objectiveType"),
            type=raw.get("type"),
            cost_type=raw.get("costType"),
            daily_budget=Money.from_api(raw.get("dailyBudget")),
            total_budget=Money.from_api(raw.get("totalBudget")),
            unit_cost=Money.from_api(raw.get("unitCost")),
            start=epoch_ms_to_iso(schedule.get("start")),
            end=epoch_ms_to_iso(schedule.get("end")),
            targeting=targeting_summary(raw.get("targetingCriteria")),
            raw=raw,
        )


@dataclass(frozen=True)
class Creative(Model):
    """A creative (an ad)."""

    id: str
    name: str | None
    campaign_id: str
    intended_status: str
    is_serving: bool | None
    serving_hold_reasons: list[str]
    review_status: str | None
    content_reference: str | None
    lead_form_id: str
    created_at: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Creative:
        leadgen = raw.get("leadgenCallToAction") or {}
        return cls(
            id=urn_id(raw.get("id")),
            name=raw.get("name"),
            campaign_id=urn_id(raw.get("campaign")),
            intended_status=raw.get("intendedStatus", ""),
            is_serving=raw.get("isServing"),
            serving_hold_reasons=list(raw.get("servingHoldReasons") or []),
            review_status=(raw.get("review") or {}).get("status"),
            content_reference=(raw.get("content") or {}).get("reference"),
            lead_form_id=urn_id(leadgen.get("destination")),
            created_at=epoch_ms_to_iso(raw.get("createdAt")),
            raw=raw,
        )


@dataclass(frozen=True)
class TargetingEntity(Model):
    urn: str
    name: str
    facet_urn: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> TargetingEntity:
        return cls(raw.get("urn", ""), raw.get("name", ""), raw.get("facetUrn"), raw)


@dataclass(frozen=True)
class AdLibraryAd(Model):
    """One ad from LinkedIn's public Ad Library.

    Live (Oct 2026) records carry only ``isRestricted``, ``adUrl``,
    ``details.type``, ``details.advertiser`` and ``details.adTargeting``;
    ``impressions`` is filled only when LinkedIn includes ``adStatistics``.
    """

    ad_url: str
    ad_type: str | None
    advertiser_name: str | None
    payer: str | None
    advertiser_url: str | None
    is_restricted: bool
    targeting: list
    impressions: dict | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> AdLibraryAd:
        details = raw.get("details") or {}
        advertiser = details.get("advertiser") or {}
        statistics = details.get("adStatistics") or {}
        return cls(
            ad_url=raw.get("adUrl", ""),
            ad_type=details.get("type"),
            advertiser_name=advertiser.get("advertiserName"),
            payer=advertiser.get("adPayer"),
            advertiser_url=advertiser.get("advertiserUrl"),
            is_restricted=bool(raw.get("isRestricted", False)),
            targeting=list(details.get("adTargeting") or []),
            impressions=statistics.get("totalImpressions"),
            raw=raw,
        )


@dataclass(frozen=True)
class LeadForm(Model):
    id: int
    name: str
    state: str
    owner: dict
    questions: list[dict]
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> LeadForm:
        content = raw.get("content") or {}
        questions = [
            {
                "question_id": question.get("questionId"),
                "name": question.get("name"),
                "predefined_field": question.get("predefinedField"),
            }
            for question in content.get("questions") or []
        ]
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name", ""),
            state=raw.get("state", ""),
            owner=raw.get("owner") or {},
            questions=questions,
            raw=raw,
        )


def _answer_value(answer: dict) -> object:
    """The answer text or chosen option ids (docs show ``answerDetails``; the
    schema table says ``accepted``, so both are read)."""
    details = answer.get("answerDetails") or answer.get("accepted") or {}
    text = details.get("textQuestionAnswer") or {}
    if "answer" in text:
        return text["answer"]
    choice = details.get("multipleChoiceAnswer") or {}
    return choice.get("options")


@dataclass(frozen=True)
class LeadFormResponse(Model):
    """One lead. ``answers`` maps question id (or field name, when the form
    projection is present) to the submitted value."""

    id: str
    submitted_at: str | None
    lead_type: str
    form_urn: str
    test_lead: bool
    campaign: str | None
    answers: dict
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> LeadFormResponse:
        names = _question_names(raw.get("form") or {})
        answers = {}
        for answer in (raw.get("formResponse") or {}).get("answers") or []:
            question_id = answer.get("questionId")
            answers[names.get(question_id, str(question_id))] = _answer_value(answer)
        metadata = (raw.get("leadMetadata") or {}).get("sponsoredLeadMetadata") or {}
        return cls(
            id=str(raw.get("id", "")),
            submitted_at=epoch_ms_to_iso(raw.get("submittedAt")),
            lead_type=str(raw.get("leadType", "")),
            form_urn=raw.get("versionedLeadGenFormUrn", ""),
            test_lead=bool(raw.get("testLead", False)),
            campaign=metadata.get("campaign"),
            answers=answers,
            raw=raw,
        )


def _question_names(form: dict) -> dict:
    """``{questionId: predefinedField or name}`` from a projected form."""
    names = {}
    for question in (form.get("content") or {}).get("questions") or []:
        label = question.get("predefinedField") or question.get("name")
        if label:
            names[question.get("questionId")] = label
    return names


@dataclass(frozen=True)
class ConversionRule(Model):
    id: int
    name: str
    type: str
    enabled: bool
    conversion_method: str | None
    attribution_type: str | None
    post_click_window_days: int | None
    view_through_window_days: int | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> ConversionRule:
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name", ""),
            type=raw.get("type", ""),
            enabled=bool(raw.get("enabled", False)),
            conversion_method=raw.get("conversionMethod"),
            attribution_type=raw.get("attributionType"),
            post_click_window_days=raw.get("postClickAttributionWindowSize"),
            view_through_window_days=raw.get("viewThroughAttributionWindowSize"),
            raw=raw,
        )


@dataclass(frozen=True)
class DmpSegment(Model):
    """A Matched Audiences segment and its LinkedIn destination status."""

    id: int
    name: str
    type: str
    source_platform: str
    input_count: int | None
    destination_status: str | None
    audience_size: int | None
    matched_count: int | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> DmpSegment:
        destination = next(iter(raw.get("destinations") or []), {})
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name", ""),
            type=raw.get("type", ""),
            source_platform=raw.get("sourcePlatform", ""),
            input_count=raw.get("inputCount"),
            destination_status=destination.get("status"),
            audience_size=destination.get("audienceSize"),
            matched_count=destination.get("matchedCount"),
            raw=raw,
        )


@dataclass(frozen=True)
class Organization(Model):
    id: int
    name: str
    vanity_name: str | None
    website: str | None
    primary_type: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Organization:
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("localizedName") or _localized(raw.get("name")),
            vanity_name=raw.get("vanityName"),
            website=raw.get("localizedWebsite"),
            primary_type=raw.get("primaryOrganizationType"),
            raw=raw,
        )


def _localized(value: object) -> str:
    if isinstance(value, dict):
        localized = value.get("localized") or {}
        return next(iter(localized.values()), "")
    return str(value or "")


@dataclass(frozen=True)
class Post(Model):
    id: str
    author: str
    commentary: str
    lifecycle_state: str | None
    visibility: str | None
    created_at: str | None
    published_at: str | None
    is_sponsored: bool
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Post:
        return cls(
            id=raw.get("id", ""),
            author=raw.get("author", ""),
            commentary=raw.get("commentary", ""),
            lifecycle_state=raw.get("lifecycleState"),
            visibility=raw.get("visibility"),
            created_at=epoch_ms_to_iso(raw.get("createdAt")),
            published_at=epoch_ms_to_iso(raw.get("publishedAt")),
            is_sponsored=bool(raw.get("adContext")),
            raw=raw,
        )


@dataclass(frozen=True)
class Event(Model):
    id: int
    name: str
    organizer: str
    starts_at: str | None
    vanity_name: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Event:
        return cls(
            id=int(raw.get("id", 0)),
            name=_localized(raw.get("name")),
            organizer=raw.get("organizer", ""),
            starts_at=epoch_ms_to_iso(raw.get("startsAt")),
            vanity_name=raw.get("vanityName"),
            raw=raw,
        )
