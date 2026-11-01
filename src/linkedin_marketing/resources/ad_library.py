"""LinkedIn's public Ad Library: search ads anyone has run on LinkedIn.

Needs the app to be approved for the "LinkedIn Ad Library" product; no extra
OAuth scope beyond a normal 3-legged member token. Verified live on
LinkedIn-Version 202610 (Oct 2026):

- ``GET /rest/adLibrary?q=criteria`` with ``keyword``, ``advertiser`` and/or
  ``payerName``; ``countries=List(urn%3Ali%3Acountry%3Agb)`` (bare ``gb``
  is rejected as an invalid URN);
- ``start``/``count`` paging with ``count`` at most 25 (more is a 400);
- records carry only ``isRestricted``, ``adUrl``, ``details.type``,
  ``details.advertiser{advertiserName, adPayer, advertiserUrl}`` and
  ``details.adTargeting`` by default;
- the quota is small and unpublished (on 31 Oct 2026 a burst of about a
  dozen calls drew 429s; whether that was a per-minute throttle or the daily
  quota was not recorded). A 200 carries no quota headers, so only the 429
  says which limit fired: ``RateLimitError`` keeps its period, message,
  headers and body. A 429 after the first page returns the ads already read
  with ``stopped="rate_limited"`` and those details; one on the first page
  raises. Every search is also capped by ``max_pages``.

The ``dateRange`` (end exclusive) parameter follows the same Rest.li date
shape as reporting; it is not in Microsoft Learn's Marketing docs, so treat
it as unverified. Ads are covered from 1 June 2023 and kept for a year
after their last impression.

Docs: https://www.linkedin.com/ad-library (product), and
https://learn.microsoft.com/linkedin/shared/api-guide/concepts/rate-limits
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from linkedin_marketing import restli
from linkedin_marketing.errors import RateLimitError
from linkedin_marketing.models import AdLibraryAd
from linkedin_marketing.resources.base import Resource
from linkedin_marketing.transport import Request

MAX_PAGE_SIZE = 25
_COUNTRY_CODE = re.compile(r"^[A-Za-z]{2}$")


def country_list(countries: Iterable[str]) -> str:
    """``List(urn%3Ali%3Acountry%3Agb,...)`` from ISO-2 codes (or country URNs)."""
    urns = []
    for country in countries:
        country = country.strip()
        if country.startswith("urn:li:country:"):
            urns.append(country.lower())
            continue
        if not _COUNTRY_CODE.match(country):
            raise ValueError(f"{country!r} is not a two-letter country code.")
        urns.append(f"urn:li:country:{country.lower()}")
    return restli.urn_list(dict.fromkeys(urns))


@dataclass(frozen=True)
class AdLibraryPage:
    """Ads found, plus where to resume (``next_start``) if more exist."""

    ads: list[AdLibraryAd]
    next_start: int | None
    total: int | None
    stopped: str | None = None
    rate_limit: dict | None = None

    def to_dict(self) -> dict:
        page = {
            "ads": [ad.to_dict() for ad in self.ads],
            "next_start": self.next_start,
            "total": self.total,
        }
        if self.stopped:
            page["stopped"] = self.stopped
            page["rate_limit"] = self.rate_limit
        return page


class AdLibrary(Resource):
    """The ``ad_library`` module."""

    def search(
        self,
        keyword: str | None = None,
        advertiser: str | None = None,
        payer: str | None = None,
        countries: Iterable[str] = (),
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = MAX_PAGE_SIZE,
        start: int = 0,
    ) -> AdLibraryPage:
        """Search the Ad Library; at least one of keyword/advertiser/payer.

        Fetches pages of up to 25 until ``limit`` ads, the last page, or
        ``LINKEDIN_MAX_PAGES``. Resume with ``start=page.next_start``. A 429
        after the first page stops early and keeps the ads already read
        (``stopped="rate_limited"``, ``rate_limit`` = what LinkedIn said);
        a 429 on the first page raises ``RateLimitError``.
        """
        query = self._query(keyword, advertiser, payer, countries, start_date, end_date)
        ads: list[AdLibraryAd] = []
        total = None
        offset = start
        more = False
        for _ in range(self._settings.max_pages):
            count = min(MAX_PAGE_SIZE, limit - len(ads))
            page = (*query, ("start", str(offset)), ("count", str(count)))
            try:
                body = self._transport.send(Request("GET", "/adLibrary", page)).body
            except RateLimitError as exc:
                if offset == start:
                    raise
                return AdLibraryPage(ads[:limit], offset, total, "rate_limited", exc.details())
            elements = body.get("elements", [])
            ads.extend(AdLibraryAd.from_api(raw) for raw in elements)
            offset += len(elements)
            total = (body.get("paging") or {}).get("total", total)
            more = _more(elements, count, offset, total)
            if not more or len(ads) >= limit:
                break
        next_start = offset if more else None
        return AdLibraryPage(ads[:limit], next_start, total)

    @staticmethod
    def _query(keyword, advertiser, payer, countries, start_date, end_date):
        if not any((keyword, advertiser, payer)):
            raise ValueError("Give at least one of keyword, advertiser or payer.")
        query: list[tuple[str, str]] = [("q", "criteria")]
        for name, value in (("keyword", keyword), ("advertiser", advertiser), ("payerName", payer)):
            if value:
                query.append((name, restli.encode(value.strip())))
        countries = list(countries)
        if countries:
            query.append(("countries", country_list(countries)))
        if start_date:
            query.append(("dateRange", restli.date_range(start_date, end_date)))
        return tuple(query)


def _more(elements: list, count: int, offset: int, total: int | None) -> bool:
    if not elements:
        return False
    if isinstance(total, int):
        return offset < total
    return len(elements) >= count
