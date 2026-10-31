"""Lead Sync: lead gen forms and their responses (leads). EXPERIMENTAL.

Needs the "Lead Sync API" product and the ``r_marketing_leadgen_automation``
scope; ``r_ads`` alone can read forms but not responses. The member needs a
role on the ad account *and* on the linked company page, or LinkedIn
answers 403. Built from the docs and LinkedIn's official Postman collection
("Lead Sync", request "8. [PULL] Schedule periodic form response (lead)
pulls"); not yet verified against the live API.

Owners are Rest.li unions: ``(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A1)``
or ``(organization:urn%3Ali%3Aorganization%3A1)``. Lead types are SPONSORED,
COMPANY, EVENT and ORGANIZATION_PRODUCT. LinkedIn keeps leads for 365 days.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/lead-sync/leadsync
https://learn.microsoft.com/linkedin/marketing/lead-sync/lead-sync-schema
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from linkedin_marketing import restli
from linkedin_marketing.models import LeadForm, LeadFormResponse
from linkedin_marketing.resources.base import Resource, take
from linkedin_marketing.transport import Request

PAGE_SIZE = 100
LEAD_TYPES = ("SPONSORED", "COMPANY", "EVENT", "ORGANIZATION_PRODUCT")
RESPONSE_PROJECTION = (
    "ownerInfo,associatedEntityInfo,leadMetadataInfo,owner,leadType,"
    "versionedLeadGenFormUrn,id,submittedAt,testLead,formResponse,"
    "form:(hiddenFields,creationLocale,name,id,content)"
)


class Leads(Resource):
    """The ``leads`` module."""

    def _owner(self, account_id: int | None, organization_id: int | None) -> str:
        """The owner union, checked against the allow-lists; exactly one owner."""
        if (account_id is None) == (organization_id is None):
            raise ValueError("Pass exactly one of account_id or organization_id.")
        if account_id is not None:
            urn = restli.account_urn(self._guard.check_account(account_id))
            return f"(sponsoredAccount:{restli.encode(urn)})"
        urn = restli.organization_urn(self._guard.check_organization(organization_id))
        return f"(organization:{restli.encode(urn)})"

    def iter_forms(
        self, account_id: int | None = None, organization_id: int | None = None
    ) -> Iterator[LeadForm]:
        """``GET /leadForms?q=owner&owner=(...)`` (start/count paging)."""
        request = Request(
            "GET",
            "/leadForms",
            (("q", "owner"), ("owner", self._owner(account_id, organization_id))),
        )
        for raw in self._transport.iter_offset(request, PAGE_SIZE):
            yield LeadForm.from_api(raw)

    def forms(
        self,
        account_id: int | None = None,
        organization_id: int | None = None,
        limit: int | None = None,
    ) -> list[LeadForm]:
        return take(self.iter_forms(account_id, organization_id), limit)

    def form(self, form_id: int) -> LeadForm:
        """``GET /leadForms/{id}``; refused if its owner is not allow-listed."""
        form = LeadForm.from_api(self._transport.get(f"/leadForms/{int(form_id)}"))
        self._check_owner(form.owner)
        return form

    def _check_owner(self, owner: dict) -> None:
        account = restli.urn_id(owner.get("sponsoredAccount"))
        organization = restli.urn_id(owner.get("organization"))
        if account.isdigit():
            self._guard.check_account(account)
        if organization.isdigit():
            self._guard.check_organization(organization)

    def iter_responses(
        self,
        account_id: int | None = None,
        organization_id: int | None = None,
        lead_type: str = "SPONSORED",
        form_id: int | None = None,
        form_version: int = 1,
        submitted_after: datetime | None = None,
        submitted_before: datetime | None = None,
        test_leads_only: bool = False,
    ) -> Iterator[LeadFormResponse]:
        """``GET /leadFormResponses?q=owner`` with the recommended projection."""
        lead_type = lead_type.upper()
        if lead_type not in LEAD_TYPES:
            raise ValueError(f"lead_type must be one of {', '.join(LEAD_TYPES)}.")
        query = [
            ("q", "owner"),
            ("owner", self._owner(account_id, organization_id)),
            ("leadType", f"(leadType:{lead_type})"),
            ("limitedToTestLeads", "true" if test_leads_only else "false"),
            ("fields", RESPONSE_PROJECTION),
        ]
        if form_id is not None:
            form = f"urn:li:leadGenForm:{int(form_id)}"
            form_urn = f"urn:li:versionedLeadGenForm:({form},{int(form_version)})"
            query.append(("versionedLeadGenFormUrn", restli.encode(form_urn)))
        if submitted_after or submitted_before:
            query.append(("submittedAtTimeRange", _time_range(submitted_after, submitted_before)))
        request = Request("GET", "/leadFormResponses", tuple(query))
        for raw in self._transport.iter_offset(request, PAGE_SIZE):
            yield LeadFormResponse.from_api(raw)

    def responses(self, *args, limit: int | None = None, **kwargs) -> list[LeadFormResponse]:
        """Leads as a list; same arguments as ``iter_responses`` plus ``limit``."""
        return take(self.iter_responses(*args, **kwargs), limit)

    def response(self, response_id: str) -> LeadFormResponse:
        """``GET /leadFormResponses/{id}`` with the recommended projection."""
        body = self._transport.get(
            f"/leadFormResponses/{restli.encode(response_id)}", (("fields", RESPONSE_PROJECTION),)
        )
        return LeadFormResponse.from_api(body)


def _time_range(after: datetime | None, before: datetime | None) -> str:
    """``(start:ms,end:ms)``; an open end defaults to now."""
    start_ms = int(after.timestamp() * 1000) if after else 0
    end_ms = int((before or datetime.now().astimezone()).timestamp() * 1000)
    return restli.time_range_ms(start_ms, end_ms)
