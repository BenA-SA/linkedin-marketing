"""Matched Audiences: DMP segments and streaming contacts/companies. EXPERIMENTAL.

Needs the Matched Audiences ("Audiences API") product and the
``rw_dmp_segments`` scope. Built from the docs and LinkedIn's official
Postman collection ("Audiences": "6. Create a User DMP segment", "7. Stream
user data", "7. Stream company data"); not yet verified against the live API.

Adds and removes use ``X-RestLi-Method: BATCH_CREATE`` with up to 5,000
elements and ``action`` ADD or REMOVE; LinkedIn reports per-element status,
so a batch can partly succeed. Emails are SHA-256 hashed locally. Wait about
5 seconds after creating a segment before streaming members into it. A
segment serves only once at least 300 members match. Deleting segments is
deliberately not offered.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/integrations/matched-audiences/create-and-manage-segments
https://learn.microsoft.com/linkedin/marketing/integrations/matched-audiences/create-and-manage-segment-users
https://learn.microsoft.com/linkedin/marketing/integrations/matched-audiences/create-and-manage-segment-companies
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from linkedin_marketing import restli
from linkedin_marketing.errors import WriteRefusedError
from linkedin_marketing.hashing import hash_email
from linkedin_marketing.models import DmpSegment
from linkedin_marketing.resources.base import Resource, require_choice, take
from linkedin_marketing.safety import WritePlan, WriteResult
from linkedin_marketing.transport import Request

MAX_BATCH = 5000
PAGE_SIZE = 100
SEGMENT_TYPES = ("USER", "COMPANY")


@dataclass(frozen=True)
class AudienceUser:
    """A contact to add or remove. ``email`` may be plain (hashed here) or SHA-256 hex."""

    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    company: str | None = None
    country: str | None = None

    def to_api(self, action: str) -> dict:
        element: dict = {"action": action}
        if self.email:
            element["userIds"] = [{"idType": "SHA256_EMAIL", "idValue": hash_email(self.email)}]
        optional = {
            "firstName": self.first_name,
            "lastName": self.last_name,
            "title": self.title,
            "company": self.company,
            "country": self.country,
        }
        element.update({key: value for key, value in optional.items() if value})
        if "userIds" not in element and not (self.first_name and self.last_name):
            raise ValueError("Each user needs an email, or a first and last name.")
        return element


@dataclass(frozen=True)
class AudienceCompany:
    """A company to add or remove; give at least one identifying field."""

    name: str | None = None
    website_domain: str | None = None
    email_domain: str | None = None
    organization_id: int | None = None
    page_url: str | None = None
    country: str | None = None

    def to_api(self, action: str) -> dict:
        fields = {
            "companyName": self.name,
            "companyWebsiteDomain": self.website_domain,
            "companyEmailDomain": self.email_domain,
            "organizationUrn": restli.organization_urn(self.organization_id)
            if self.organization_id
            else None,
            "companyPageUrl": self.page_url,
        }
        identifiers = {key: value for key, value in fields.items() if value}
        if not identifiers:
            raise ValueError("Each company needs a name, domain, organization id or page URL.")
        element = {"action": action, **identifiers}
        if self.country:
            element["country"] = self.country
        return element


class Audiences(Resource):
    """The ``audiences`` module."""

    def segments(self, account_id: int, limit: int | None = None) -> list[DmpSegment]:
        """``GET /dmpSegments?q=account&account={urn}`` (start/count paging)."""
        account_id = self._guard.check_account(account_id)
        request = Request(
            "GET",
            "/dmpSegments",
            (("q", "account"), ("account", restli.encode(restli.account_urn(account_id)))),
        )
        rows = self._transport.iter_offset(request, PAGE_SIZE)
        return take((DmpSegment.from_api(raw) for raw in rows), limit)

    def segment(self, segment_id: int) -> DmpSegment:
        """``GET /dmpSegments/{id}``; refused if its account is not allow-listed."""
        segment = DmpSegment.from_api(self._transport.get(f"/dmpSegments/{int(segment_id)}"))
        self._guard.check_account(restli.urn_id(segment.raw.get("account")) or 0)
        return segment

    def create_segment(
        self,
        account_id: int,
        name: str,
        segment_type: str = "USER",
        description: str = "",
        confirm: bool = False,
    ) -> WriteResult:
        """Create a streaming segment (``DIRECT_API``). Dry run by default."""
        action = "create_dmp_segment"
        account_id = self._guard.check_account(account_id)
        segment_type = require_choice(segment_type, SEGMENT_TYPES, action, "segment_type")
        body = {
            "name": name,
            "sourcePlatform": "DIRECT_API",
            "account": restli.account_urn(account_id),
            "type": segment_type,
            "destinations": [{"destination": "LINKEDIN"}],
        }
        if description:
            body["description"] = description
        plan = WritePlan(
            action=action,
            request=Request("POST", "/dmpSegments", json=body),
            target={"account_id": account_id, "name": name},
            summary={"segment_type": segment_type},
        )
        return self._guard.execute(plan, confirm)

    def add_users(
        self, segment_id: int, users: Iterable[AudienceUser], confirm: bool = False
    ) -> WriteResult:
        return self._stream(segment_id, "users", "USER", "ADD", list(users), confirm)

    def remove_users(
        self, segment_id: int, users: Iterable[AudienceUser], confirm: bool = False
    ) -> WriteResult:
        return self._stream(segment_id, "users", "USER", "REMOVE", list(users), confirm)

    def add_companies(
        self, segment_id: int, companies: Iterable[AudienceCompany], confirm: bool = False
    ) -> WriteResult:
        return self._stream(segment_id, "companies", "COMPANY", "ADD", list(companies), confirm)

    def remove_companies(
        self, segment_id: int, companies: Iterable[AudienceCompany], confirm: bool = False
    ) -> WriteResult:
        return self._stream(segment_id, "companies", "COMPANY", "REMOVE", list(companies), confirm)

    def _stream(self, segment_id, member_path, segment_type, verb, members, confirm) -> WriteResult:
        """Plan a BATCH_CREATE of ADD/REMOVE elements on a verified segment."""
        action = f"{verb.lower()}_segment_{member_path}"
        if not members or len(members) > MAX_BATCH:
            raise WriteRefusedError(
                "batch_too_large",
                action,
                f"send between 1 and {MAX_BATCH} per call",
                count=len(members),
            )
        segment = self.segment(segment_id)
        if segment.type != segment_type:
            raise WriteRefusedError(
                "invalid_input",
                action,
                f"segment {segment.id} is a {segment.type} segment, not {segment_type}",
                segment_id=segment.id,
            )
        try:
            elements = [member.to_api(verb) for member in members]
        except ValueError as exc:
            raise WriteRefusedError("invalid_input", action, str(exc)) from exc
        plan = WritePlan(
            action=action,
            request=Request(
                "POST",
                f"/dmpSegments/{segment.id}/{member_path}",
                json={"elements": elements},
                headers={"X-RestLi-Method": "BATCH_CREATE"},
            ),
            target={"segment_id": segment.id, "segment_name": segment.name},
            summary={"member_count": len(elements)},
        )
        return self._guard.execute(plan, confirm)
