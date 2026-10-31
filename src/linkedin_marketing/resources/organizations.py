"""Community Management: pages, posts, social metadata, statistics. EXPERIMENTAL.

Needs the "Community Management API" product. Scopes: ``r_organization_social``
(read posts), ``w_organization_social`` (post), ``r_organization_admin`` /
``rw_organization_admin`` (page details and statistics; the statistics docs
list only ``rw_organization_admin``). The member must be an ADMINISTRATOR
(or a content role, for posting) on the page. Built from the docs; not yet
verified against the live API.

Statistics windows: follower gains run from 12 months ago to 2 days ago
(earlier starts are clamped); share statistics are organic only over a
rolling 12 months. ``totalFollowerCounts`` no longer exists, so the follower
total comes from ``networkSizes``.

Posting is the only write. Commentary uses LinkedIn's "little" text format,
so reserved characters are backslash-escaped by default. Editing and
deleting posts are deliberately not offered.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/community-management/organizations/organization-lookup-api
https://learn.microsoft.com/linkedin/marketing/community-management/organizations/organization-access-control-by-role
https://learn.microsoft.com/linkedin/marketing/community-management/shares/posts-api
https://learn.microsoft.com/linkedin/marketing/community-management/shares/little-text-format
https://learn.microsoft.com/linkedin/marketing/community-management/shares/social-metadata-api
https://learn.microsoft.com/linkedin/marketing/community-management/organizations/follower-statistics
https://learn.microsoft.com/linkedin/marketing/community-management/organizations/page-statistics
https://learn.microsoft.com/linkedin/marketing/community-management/organizations/share-statistics
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from linkedin_marketing import restli
from linkedin_marketing.errors import WriteRefusedError
from linkedin_marketing.models import Organization, Post
from linkedin_marketing.resources.base import Resource, require_choice, take
from linkedin_marketing.safety import WritePlan, WriteResult
from linkedin_marketing.transport import Request

POSTS_PAGE_SIZE = 100
ACL_PAGE_SIZE = 100
MAX_POST_CHARS = 3000
LITTLE_RESERVED = "\\|{}@[]()<>#*_~"
GRANULARITIES = ("DAY", "WEEK", "MONTH")
POST_WARNING = (
    "This publishes a PUBLIC post on the organization's LinkedIn page, visible "
    "to its followers immediately. Check the text before confirming."
)


def escape_little(text: str) -> str:
    """Backslash-escape every reserved character of the "little" text format."""
    return "".join(f"\\{char}" if char in LITTLE_RESERVED else char for char in text)


def _time_intervals(start: date, end: date | None, granularity: str) -> str:
    end_part = f",end:{restli.epoch_ms(end)}" if end else ""
    return (
        f"(timeRange:(start:{restli.epoch_ms(start)}{end_part}),timeGranularityType:{granularity})"
    )


class Organizations(Resource):
    """The ``organizations`` module."""

    def _org_urn(self, organization_id: int) -> str:
        return restli.organization_urn(self._guard.check_organization(organization_id))

    def administered(self, role: str = "ADMINISTRATOR", limit: int | None = None) -> list[dict]:
        """Pages the member holds ``role`` on: ``GET /organizationAcls?q=roleAssignee``."""
        request = Request(
            "GET",
            "/organizationAcls",
            (("q", "roleAssignee"), ("role", role.upper()), ("state", "APPROVED")),
        )
        rows = (
            {
                "organization": raw.get("organization") or raw.get("organizationTarget"),
                "organization_id": restli.urn_id(
                    raw.get("organization") or raw.get("organizationTarget")
                ),
                "role": raw.get("role"),
                "state": raw.get("state"),
            }
            for raw in self._transport.iter_offset(request, ACL_PAGE_SIZE)
        )
        allowed = {str(i) for i in self._settings.allowed_organization_ids}
        return take(
            (row for row in rows if not allowed or row["organization_id"] in allowed), limit
        )

    def get(self, organization_id: int) -> Organization:
        """``GET /organizations/{id}`` (admins) for an allow-listed page."""
        organization_id = self._guard.check_organization(organization_id)
        return Organization.from_api(self._transport.get(f"/organizations/{organization_id}"))

    def lookup_vanity_name(self, vanity_name: str) -> list[Organization]:
        """Public page lookup: ``GET /organizations?q=vanityName``."""
        body = self._transport.get(
            "/organizations", (("q", "vanityName"), ("vanityName", restli.encode(vanity_name)))
        )
        return [Organization.from_api(raw) for raw in body.get("elements", [])]

    def follower_count(self, organization_id: int) -> int | None:
        """``GET /networkSizes/{org urn}?edgeType=COMPANY_FOLLOWED_BY_MEMBER``."""
        urn = restli.encode(self._org_urn(organization_id))
        body = self._transport.get(
            f"/networkSizes/{urn}", (("edgeType", "COMPANY_FOLLOWED_BY_MEMBER"),)
        )
        return body.get("firstDegreeSize")

    def posts(
        self, organization_id: int, limit: int | None = 20, sort_by: str = "LAST_MODIFIED"
    ) -> list[Post]:
        """The page's posts (organic and sponsored): ``GET /posts?q=author``."""
        request = Request(
            "GET",
            "/posts",
            (
                ("q", "author"),
                ("author", restli.encode(self._org_urn(organization_id))),
                ("sortBy", sort_by.upper()),
            ),
            headers={"X-RestLi-Method": "FINDER"},
        )
        rows = self._transport.iter_offset(request, POSTS_PAGE_SIZE)
        return take((Post.from_api(raw) for raw in rows), limit)

    def post(self, post_urn: str) -> Post:
        """``GET /posts/{encoded urn}`` (share or ugcPost URN)."""
        return Post.from_api(self._transport.get(f"/posts/{restli.encode(post_urn)}"))

    def social_metadata(self, post_urns: Iterable[str]) -> dict:
        """Reaction and comment counts per post: ``GET /socialMetadata?ids=List(...)``."""
        urns = list(dict.fromkeys(post_urns))
        if not urns:
            return {}
        body = self._transport.get("/socialMetadata", (("ids", restli.urn_list(urns)),))
        results = body.get("results") or {}
        return {urn: _social_summary(results.get(urn) or {}) for urn in urns}

    def follower_statistics(
        self,
        organization_id: int,
        start: date | None = None,
        end: date | None = None,
        granularity: str = "DAY",
    ) -> list[dict]:
        """Lifetime follower breakdowns, or gains per period when ``start`` is given."""
        query = [
            ("q", "organizationalEntity"),
            ("organizationalEntity", restli.encode(self._org_urn(organization_id))),
        ]
        if start:
            query.append(("timeIntervals", _time_intervals(start, end, _granularity(granularity))))
        body = self._transport.get("/organizationalEntityFollowerStatistics", query)
        return body.get("elements", [])

    def page_statistics(
        self,
        organization_id: int,
        start: date | None = None,
        end: date | None = None,
        granularity: str = "DAY",
    ) -> list[dict]:
        """Page views and clicks: ``GET /organizationPageStatistics?q=organization``."""
        query = [
            ("q", "organization"),
            ("organization", restli.encode(self._org_urn(organization_id))),
        ]
        if start:
            query.append(("timeIntervals", _time_intervals(start, end, _granularity(granularity))))
        body = self._transport.get("/organizationPageStatistics", query)
        return body.get("elements", [])

    def share_statistics(
        self,
        organization_id: int,
        start: date | None = None,
        end: date | None = None,
        granularity: str = "DAY",
        post_urns: Iterable[str] = (),
    ) -> list[dict]:
        """Organic impressions, clicks and engagement for the page or given posts.

        Per-post statistics are lifetime only, so ``post_urns`` cannot be
        combined with a date range.
        """
        query = [
            ("q", "organizationalEntity"),
            ("organizationalEntity", restli.encode(self._org_urn(organization_id))),
        ]
        urns = list(post_urns)
        if urns and start:
            raise ValueError("Per-post share statistics are lifetime only; drop start/end.")
        shares = [u for u in urns if u.startswith("urn:li:share:")]
        ugc_posts = [u for u in urns if u.startswith("urn:li:ugcPost:")]
        if shares:
            query.append(("shares", restli.urn_list(shares)))
        if ugc_posts:
            query.append(("ugcPosts", restli.urn_list(ugc_posts)))
        if start:
            query.append(("timeIntervals", _time_intervals(start, end, _granularity(granularity))))
        body = self._transport.get("/organizationalEntityShareStatistics", query)
        return body.get("elements", [])

    def create_post(
        self,
        organization_id: int,
        text: str,
        link_url: str | None = None,
        link_title: str | None = None,
        link_description: str | None = None,
        escape: bool = True,
        confirm: bool = False,
    ) -> WriteResult:
        """Publish a text (or link) post as the page. Dry run by default.

        ``escape=True`` escapes the "little" format's reserved characters so
        text is posted literally; pass False only if ``text`` already uses
        mention/hashtag markup. The new post URN comes back as ``created_id``.
        """
        action = "create_organization_post"
        if not text.strip() or len(text) > MAX_POST_CHARS:
            raise WriteRefusedError(
                "invalid_input",
                action,
                f"text must be 1-{MAX_POST_CHARS} characters",
                length=len(text),
            )
        author = self._org_urn(organization_id)
        body: dict = {
            "author": author,
            "commentary": escape_little(text) if escape else text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if link_url:
            article = {"source": link_url, "title": link_title or link_url}
            if link_description:
                article["description"] = link_description
            body["content"] = {"article": article}
        plan = WritePlan(
            action=action,
            request=Request("POST", "/posts", json=body),
            target={"organization_id": int(organization_id)},
            summary={"characters": len(text), "has_link": bool(link_url)},
            warning=POST_WARNING,
        )
        return self._guard.execute(plan, confirm)


def _granularity(value: str) -> str:
    return require_choice(value, GRANULARITIES, "statistics", "granularity")


def _social_summary(raw: dict) -> dict:
    reactions = {
        name: (summary or {}).get("count", 0)
        for name, summary in (raw.get("reactionSummaries") or {}).items()
    }
    comments = raw.get("commentSummary") or {}
    return {
        "reactions": reactions,
        "reaction_total": sum(reactions.values()),
        "comments": comments.get("count", 0),
        "top_level_comments": comments.get("topLevelCount", 0),
        "comments_state": raw.get("commentsState"),
    }
