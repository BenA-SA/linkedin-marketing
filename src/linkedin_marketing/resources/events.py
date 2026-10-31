"""LinkedIn Events organised by a page. EXPERIMENTAL, read-only.

Needs the separate "Events Management API" product and ``r_events``; the
member must administer the organizing page. Built from the docs; not yet
verified against the live API. Creating events is not offered.

Docs (LinkedIn-Version 202610):
https://learn.microsoft.com/linkedin/marketing/event-management/events
"""

from __future__ import annotations

from linkedin_marketing import restli
from linkedin_marketing.models import Event
from linkedin_marketing.resources.base import Resource, require_choice, take
from linkedin_marketing.transport import Request

PAGE_SIZE = 50
LIFECYCLE_STATES = ("UPCOMING", "ONGOING", "PAST")


class Events(Resource):
    """The ``events`` module."""

    def by_organizer(
        self, organization_id: int, lifecycle_state: str = "UPCOMING", limit: int | None = 50
    ) -> list[Event]:
        """``GET /events?q=eventsByOrganizer`` filtered by lifecycle state."""
        organization_id = self._guard.check_organization(organization_id)
        state = require_choice(lifecycle_state, LIFECYCLE_STATES, "list_events", "lifecycle_state")
        sort = "END_TIME_DESC" if state == "PAST" else "START_TIME_ASC"
        request = Request(
            "GET",
            "/events",
            (
                ("q", "eventsByOrganizer"),
                ("organizer", restli.encode(restli.organization_urn(organization_id))),
                ("excludeCancelled", "true"),
                ("timeBasedFilter", f"(lifeCycleState:{state})"),
                ("sortOrder", sort),
            ),
        )
        rows = self._transport.iter_offset(request, PAGE_SIZE)
        return take((Event.from_api(raw) for raw in rows), limit)

    def get(self, event_id: int) -> Event:
        """``GET /events/{id}``; refused if the organizer is not allow-listed."""
        event = Event.from_api(self._transport.get(f"/events/{int(event_id)}"))
        organizer_id = restli.urn_id(event.organizer)
        if organizer_id.isdigit():
            self._guard.check_organization(organizer_id)
        return event
