"""The switchable modules: their OAuth scopes, LinkedIn product and test status.

A module maps to one LinkedIn developer *product* your app must be approved
for. Only enable the modules your app has; the client refuses to use the
others (``ModuleDisabledError``) and the MCP server does not register their
tools. ``mint-token`` uses ``scopes`` to request exactly what you enable.

``status`` is ``live-tested`` when the requests were verified against the
real API, ``experimental`` when they are built from the documentation and
unit-tested against mocked HTTP only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    """One switchable area of the API."""

    name: str
    product: str
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    status: str
    docs: str

    @property
    def scopes(self) -> tuple[str, ...]:
        """Every scope the module can use, reads first."""
        return (*self.read_scopes, *self.write_scopes)

    @property
    def experimental(self) -> bool:
        return self.status != "live-tested"


_DOCS = "https://learn.microsoft.com/linkedin/marketing"

MODULES: dict[str, Module] = {
    module.name: module
    for module in (
        Module(
            "ads",
            "Advertising API",
            ("r_ads",),
            ("rw_ads",),
            "live-tested",
            f"{_DOCS}/integrations/ads/getting-started",
        ),
        Module(
            "reporting",
            "Advertising API",
            ("r_ads_reporting",),
            (),
            "live-tested",
            f"{_DOCS}/integrations/ads-reporting/ads-reporting",
        ),
        Module(
            "ad_library",
            "LinkedIn Ad Library",
            (),
            (),
            "live-tested",
            f"{_DOCS}/integrations/ads/ad-library",
        ),
        Module(
            "leads",
            "Lead Sync API",
            ("r_marketing_leadgen_automation",),
            (),
            "experimental",
            f"{_DOCS}/lead-sync/leadsync",
        ),
        Module(
            "conversions",
            "Conversions API",
            ("r_ads",),
            ("rw_conversions",),
            "experimental",
            f"{_DOCS}/integrations/ads-reporting/conversions-api",
        ),
        Module(
            "audiences",
            "Matched Audiences (Audiences API)",
            ("r_ads",),
            ("rw_dmp_segments",),
            "experimental",
            f"{_DOCS}/integrations/matched-audiences/matched-audiences",
        ),
        Module(
            "organizations",
            "Community Management API",
            ("r_organization_social", "r_organization_admin"),
            ("w_organization_social", "rw_organization_admin"),
            "experimental",
            f"{_DOCS}/community-management/community-management-overview",
        ),
        Module(
            "events",
            "Events Management API",
            ("r_events",),
            (),
            "experimental",
            f"{_DOCS}/event-management/events",
        ),
    )
}

DEFAULT_MODULES: tuple[str, ...] = ("ads", "reporting", "ad_library")


def scopes_for(modules: tuple[str, ...], include_writes: bool = True) -> list[str]:
    """The de-duplicated OAuth scopes the given modules need, in a stable order."""
    scopes: list[str] = []
    for name in modules:
        module = MODULES[name]
        wanted = module.scopes if include_writes else module.read_scopes
        scopes.extend(scope for scope in wanted if scope not in scopes)
    return scopes
