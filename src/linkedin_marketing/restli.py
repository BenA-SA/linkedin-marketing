"""Rest.li 2.0 query building, which LinkedIn is strict about.

With ``X-Restli-Protocol-Version: 2.0.0`` a URN inside a query value must be
percent-encoded (``urn%3Ali%3AsponsoredCampaign%3A1``) while the Rest.li
structure around it (``List(``, ``(``, ``:``, ``,``) must stay literal. A
generic encoder such as ``urlencode`` escapes the structure too and LinkedIn
answers 400 "Invalid query parameters". So every value is built here, already
encoded, and joined into the query string verbatim.

Docs: https://learn.microsoft.com/linkedin/shared/api-guide/concepts/protocol-version
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from urllib.parse import quote


def encode(value: str) -> str:
    """Percent-encode every reserved character (``:`` → ``%3A``, space → ``%20``)."""
    return quote(str(value), safe="")


def restli_list(values: Iterable[str]) -> str:
    """``List(a,b)`` with each value left as given (encode URNs first)."""
    return f"List({','.join(values)})"


def urn_list(urns: Iterable[str]) -> str:
    """``List(...)`` of encoded URNs."""
    return restli_list(encode(urn) for urn in urns)


def urn(kind: str, entity_id: int | str) -> str:
    """``urn:li:{kind}:{id}``."""
    return f"urn:li:{kind}:{entity_id}"


def account_urn(account_id: int) -> str:
    return urn("sponsoredAccount", account_id)


def campaign_urn(campaign_id: int) -> str:
    return urn("sponsoredCampaign", campaign_id)


def campaign_group_urn(group_id: int) -> str:
    return urn("sponsoredCampaignGroup", group_id)


def creative_urn(creative_id: int) -> str:
    return urn("sponsoredCreative", creative_id)


def organization_urn(organization_id: int) -> str:
    return urn("organization", organization_id)


def urn_id(value: str | None) -> str:
    """The trailing id of a simple URN: ``urn:li:sponsoredCampaign:12`` → ``12``."""
    return str(value).rpartition(":")[2] if value else ""


def restli_date(day: date) -> str:
    return f"(year:{day.year},month:{day.month},day:{day.day})"


def date_range(start: date, end: date | None = None) -> str:
    """``(start:(year:..,month:..,day:..),end:(...))``; ``end`` optional."""
    if end is None:
        return f"(start:{restli_date(start)})"
    return f"(start:{restli_date(start)},end:{restli_date(end)})"


def epoch_ms(day: date) -> int:
    """Midnight UTC of ``day`` in epoch milliseconds."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def time_range_ms(start_ms: int, end_ms: int) -> str:
    """``(start:ms,end:ms)``."""
    return f"(start:{int(start_ms)},end:{int(end_ms)})"


def search_criteria(**criteria: Iterable[str]) -> str:
    """``(status:(values:List(ACTIVE,PAUSED)),...)`` for ``q=search`` finders.

    Empty criteria are dropped; values must already be encoded.
    """
    parts = [
        f"{name}:(values:{restli_list(values)})" for name, values in criteria.items() if values
    ]
    return f"({','.join(parts)})"


def build_query(params: Iterable[tuple[str, str]]) -> str:
    """Join pre-encoded ``(name, value)`` pairs into a raw query string."""
    return "&".join(f"{name}={value}" for name, value in params)


def serialize(value: object) -> str:
    """A Python value as a Rest.li 2.0 query value, leaves encoded.

    ``dict`` → ``(key:value,...)``, ``list``/``tuple`` → ``List(...)``,
    ``bool`` → ``true``/``false``, everything else → its percent-encoded
    text. Keys are encoded too, because targeting facets use URNs as keys.
    Used for nested parameters such as ``targetingCriteria``.
    """
    if isinstance(value, dict):
        inner = ",".join(f"{encode(key)}:{serialize(item)}" for key, item in value.items())
        return f"({inner})"
    if isinstance(value, list | tuple):
        return restli_list(serialize(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return encode(str(value))
