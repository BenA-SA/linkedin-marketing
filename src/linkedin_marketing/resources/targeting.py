"""Targeting facets and entities, audience counts, and ad previews.

``targeting_criteria`` arguments use LinkedIn's JSON shape, e.g.::

    {"include": {"and": [{"or": {"urn:li:adTargetingFacet:locations":
        ["urn:li:geo:101165590"]}}]}}

and are serialised to Rest.li here (facet URNs and values percent-encoded).

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/ads/advertising-targeting/ads-targeting
https://learn.microsoft.com/linkedin/marketing/integrations/ads/advertising-targeting/audience-counts
https://learn.microsoft.com/linkedin/marketing/integrations/ads/account-structure/ad-preview
"""

from __future__ import annotations

from collections.abc import Iterable

from linkedin_marketing import restli
from linkedin_marketing.models import TargetingEntity
from linkedin_marketing.resources.base import Resource

QUERY_VERSION = "QUERY_USES_URNS"


def facet_urn(facet: str) -> str:
    """``seniorities`` → ``urn:li:adTargetingFacet:seniorities`` (URNs pass through)."""
    return facet if facet.startswith("urn:") else f"urn:li:adTargetingFacet:{facet}"


def _locale(language: str, country: str) -> str:
    return f"(language:{language},country:{country})"


class Targeting(Resource):
    """Look up what can be targeted and how big an audience is."""

    def facets(self) -> list[dict]:
        """Every targeting facet: ``GET /adTargetingFacets``."""
        body = self._transport.get("/adTargetingFacets")
        return [
            {
                "facet": raw.get("facetName"),
                "urn": raw.get("adTargetingFacetUrn"),
                "finders": raw.get("availableEntityFinders", []),
            }
            for raw in body.get("elements", [])
        ]

    def entities(
        self, facet: str, language: str = "en", country: str = "US"
    ) -> list[TargetingEntity]:
        """All values of a facet: ``GET /adTargetingEntities?q=adTargetingFacet``.

        Locations only support ``typeahead``.
        """
        body = self._transport.get(
            "/adTargetingEntities",
            (
                ("q", "adTargetingFacet"),
                ("facet", restli.encode(facet_urn(facet))),
                ("queryVersion", QUERY_VERSION),
                ("locale", _locale(language, country)),
            ),
        )
        return [TargetingEntity.from_api(raw) for raw in body.get("elements", [])]

    def typeahead(
        self,
        facet: str,
        query: str,
        entity_type: str | None = None,
        language: str = "en",
        country: str = "US",
    ) -> list[TargetingEntity]:
        """Search a facet by text: ``GET /adTargetingEntities?q=typeahead``."""
        params = [
            ("q", "typeahead"),
            ("facet", restli.encode(facet_urn(facet))),
            ("query", restli.encode(query)),
            ("queryVersion", QUERY_VERSION),
            ("locale", _locale(language, country)),
        ]
        if entity_type:
            params.append(("entityType", entity_type))
        body = self._transport.get("/adTargetingEntities", params)
        return [TargetingEntity.from_api(raw) for raw in body.get("elements", [])]

    def similar(self, facet: str, entity_urns: Iterable[str]) -> list[TargetingEntity]:
        """Entities similar to the given ones: ``q=similarEntities``."""
        body = self._transport.get(
            "/adTargetingEntities",
            (
                ("q", "similarEntities"),
                ("facet", restli.encode(facet_urn(facet))),
                ("entities", restli.urn_list(entity_urns)),
                ("queryVersion", QUERY_VERSION),
            ),
        )
        return [TargetingEntity.from_api(raw) for raw in body.get("elements", [])]

    def resolve(self, urns: Iterable[str]) -> list[TargetingEntity]:
        """Names for targeting URNs: ``GET /adTargetingEntities?q=urns``."""
        urns = list(dict.fromkeys(urns))
        if not urns:
            return []
        body = self._transport.get(
            "/adTargetingEntities",
            (("q", "urns"), ("urns", restli.urn_list(urns)), ("fetchType", "SINGLE_NODE")),
        )
        return [TargetingEntity.from_api(raw) for raw in body.get("elements", [])]

    def audience_count(self, targeting_criteria: dict) -> dict:
        """Approximate audience size: ``GET /audienceCounts?q=targetingCriteriaV2``.

        ``total`` is rounded and reported as 0 for audiences under 300.
        """
        body = self._transport.get(
            "/audienceCounts",
            (
                ("q", "targetingCriteriaV2"),
                ("targetingCriteria", restli.serialize(targeting_criteria)),
            ),
        )
        first = next(iter(body.get("elements", [])), {})
        return {"total": first.get("total"), "active": first.get("active")}


class Previews(Resource):
    """Rendered previews of an existing creative."""

    def creative(self, account_id: int, creative_id: int) -> list[dict]:
        """``GET /adPreviews?q=creative``; each preview is iframe HTML valid ~3 hours."""
        account_id = self._guard.check_account(account_id)
        body = self._transport.get(
            "/adPreviews",
            (
                ("q", "creative"),
                ("creative", restli.encode(restli.creative_urn(int(creative_id)))),
                ("account", restli.encode(restli.account_urn(account_id))),
            ),
        )
        return [
            {
                "placement": (raw.get("placement") or {}).get("linkedin", {}),
                "preview_html": raw.get("preview", ""),
            }
            for raw in body.get("elements", [])
        ]
