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
- the quota is small (roughly a dozen calls a day on a new app before a 429
  ``TOO_MANY_REQUESTS``); check the app's Analytics tab in the Developer
  Portal. Every search is therefore capped by ``max_pages``.

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

    def to_dict(self) -> dict:
        return {
            "ads": [ad.to_dict() for ad in self.ads],
            "next_start": self.next_start,
            "total": self.total,
        }


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
        ``LINKEDIN_MAX_PAGES``. Resume with ``start=page.next_start``.
        """
        query = self._query(keyword, advertiser, payer, countries, start_date, end_date)
        ads: list[AdLibraryAd] = []
        total = None
        offset = start
        more = False
        for _ in range(self._settings.max_pages):
            count = min(MAX_PAGE_SIZE, limit - len(ads))
            page = (*query, ("start", str(offset)), ("count", str(count)))
            body = self._transport.send(Request("GET", "/adLibrary", page)).body
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
