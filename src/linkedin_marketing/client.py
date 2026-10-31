"""``LinkedInClient``: one object, one attribute per module.

    client = LinkedInClient.from_env()
    client.ads.campaigns.list(123456789)
    client.reporting.analytics(123456789, start=date(2026, 10, 1))
    client.ad_library.search(keyword="data analytics", countries=["GB"])

Touching a module that is not enabled raises ``ModuleDisabledError`` before
any request is made. Use the client as a context manager (or call
``close()``) to release the HTTP connection pool.
"""

from __future__ import annotations

import httpx

from linkedin_marketing.auth import TokenProvider
from linkedin_marketing.config import Settings
from linkedin_marketing.resources.ad_library import AdLibrary
from linkedin_marketing.resources.ads import Ads
from linkedin_marketing.resources.audiences import Audiences
from linkedin_marketing.resources.conversions import Conversions
from linkedin_marketing.resources.events import Events
from linkedin_marketing.resources.leads import Leads
from linkedin_marketing.resources.organizations import Organizations
from linkedin_marketing.resources.reporting import Reporting
from linkedin_marketing.safety import Guard
from linkedin_marketing.transport import Transport

HTTP_TIMEOUT_SECONDS = 30.0
USER_AGENT = "linkedin-marketing-python (+https://github.com/BenA-SA/linkedin-marketing)"


class LinkedInClient:
    """Typed access to the LinkedIn Marketing API with built-in write safety."""

    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None):
        self.settings = settings or Settings.from_env()
        self._owns_http = http is None
        self._http = http or httpx.Client(
            timeout=HTTP_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
        )
        self.transport = Transport(
            self.settings, self._http, TokenProvider(self.settings, self._http)
        )
        self.guard = Guard(self.settings, self.transport.send)
        self._ads = Ads(self.transport, self.guard)
        self._reporting = Reporting(
            self.transport,
            self.guard,
            self._ads.campaigns,
            self._ads.campaign_groups,
            self._ads.accounts,
        )
        self._ad_library = AdLibrary(self.transport, self.guard)
        self._leads = Leads(self.transport, self.guard)
        self._conversions = Conversions(self.transport, self.guard)
        self._audiences = Audiences(self.transport, self.guard)
        self._organizations = Organizations(self.transport, self.guard)
        self._events = Events(self.transport, self.guard)

    @classmethod
    def from_env(cls) -> LinkedInClient:
        return cls(Settings.from_env())

    def _module(self, name: str, resource):
        self.guard.require_module(name)
        return resource

    @property
    def ads(self) -> Ads:
        return self._module("ads", self._ads)

    @property
    def reporting(self) -> Reporting:
        return self._module("reporting", self._reporting)

    @property
    def ad_library(self) -> AdLibrary:
        return self._module("ad_library", self._ad_library)

    @property
    def leads(self) -> Leads:
        return self._module("leads", self._leads)

    @property
    def conversions(self) -> Conversions:
        return self._module("conversions", self._conversions)

    @property
    def audiences(self) -> Audiences:
        return self._module("audiences", self._audiences)

    @property
    def organizations(self) -> Organizations:
        return self._module("organizations", self._organizations)

    @property
    def events(self) -> Events:
        return self._module("events", self._events)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> LinkedInClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
