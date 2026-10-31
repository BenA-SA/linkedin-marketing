# linkedin-marketing

A typed Python client for the **official LinkedIn Marketing API**, with a
**dry-run-by-default safety layer** and an optional **MCP server** so Claude,
Cursor and other MCP clients can use it too.

- **Library first.** `client.ads.campaigns.list(...)`, `client.reporting.analytics(...)`,
  `client.ad_library.search(...)` and more, returning typed dataclasses with
  DataFrame-friendly `to_dict()` / `to_records()`.
- **Safe writes.** Every write returns the exact request as a dry run unless you pass
  `confirm=True`. On top of that: read-only mode (on by default), allow-lists for
  ad accounts and pages, a daily-budget cap, and a local JSONL audit log. There are
  no delete endpoints, no billing, and no member-profile scraping.
- **Modular.** Switch modules on and off to match the LinkedIn products your
  app is approved for.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported
> by LinkedIn or Microsoft. "LinkedIn" is a trademark of LinkedIn Corporation. You
> need your own LinkedIn developer app, and your use must follow the
> [LinkedIn API Terms of Use](https://www.linkedin.com/legal/l/api-terms-of-use).

## Contents

- [Install](#install)
- [Python quick start](#python-quick-start)
- [Modules and status](#modules-and-status)
- [Getting a LinkedIn app and a token](#getting-a-linkedin-app-and-a-token)
- [Configuration](#configuration)
- [Safety model](#safety-model)
- [MCP server](#mcp-server)
- [Rate limits](#rate-limits)
- [Why another LinkedIn MCP?](#why-another-linkedin-mcp)
- [Known gaps in LinkedIn's docs](#known-gaps-in-linkedins-docs)
- [Contributing, security, licence](#contributing-security-licence)

## Install

Python 3.12 or newer. The package is not on PyPI yet, so install from GitHub:

```bash
# library only
uv add "linkedin-marketing @ git+https://github.com/BenA-SA/linkedin-marketing"
pip install "linkedin-marketing @ git+https://github.com/BenA-SA/linkedin-marketing"

# with the MCP server
pip install "linkedin-marketing[mcp] @ git+https://github.com/BenA-SA/linkedin-marketing"
```

The only runtime dependency is `httpx`. The `mcp` extra adds the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (1.x or 2.x).

## Python quick start

```python
from datetime import date
from linkedin_marketing import LinkedInClient

# Reads LINKEDIN_* environment variables (see Configuration).
with LinkedInClient.from_env() as client:
    accounts = client.ads.accounts.list()
    account_id = accounts[0].id                      # e.g. 123456789

    for campaign in client.ads.campaigns.list(account_id, statuses=["ACTIVE"]):
        print(campaign.id, campaign.name, campaign.daily_budget)
```

**Analytics into a DataFrame-friendly list.** Spend stays `Decimal` inside
the library and comes out as a 2dp string. CTR, CPC, CPM and CPL are derived
for you.

```python
report = client.reporting.analytics(
    account_id,
    start=date(2026, 10, 1),
    end=date(2026, 10, 31),
    pivot="CAMPAIGN",
    time_granularity="DAILY",
)
records = report.to_records()   # list[dict]: pivot_id, start, impressions, clicks,
                                # costInLocalCurrency, ctr_percent, cpc, cpm, cpl, ...
print(report.totals())

import pandas as pd             # optional, not a dependency
df = pd.DataFrame(records)

# Who saw the ads, with readable names (privacy thresholds apply, see below)
by_function = client.reporting.demographics(account_id, start=date(2026, 10, 1),
                                            pivot="MEMBER_JOB_FUNCTION")
```

**Ad Library search** (public ads from any advertiser):

```python
page = client.ad_library.search(keyword="data analytics", countries=["GB"], limit=25)
for ad in page.ads:
    print(ad.advertiser_name, ad.ad_type, ad.ad_url)
more = client.ad_library.search(keyword="data analytics", countries=["GB"],
                                start=page.next_start)
```

**A guarded write.** The first call is a dry run. The second sends the request,
but only when `LINKEDIN_READ_ONLY=false`.

```python
plan = client.ads.campaigns.set_status(account_id, 222222222, "PAUSED")
print(plan.request)      # exact method, URL, headers (no token) and JSON body
print(plan.before, "->", plan.after)

result = client.ads.campaigns.set_status(account_id, 222222222, "PAUSED", confirm=True)
```

Other modules follow the same shape:

```python
client.leads.responses(account_id=account_id, lead_type="SPONSORED", limit=50)
client.conversions.send_events(account_id, rule_id, [ConversionEvent(happened_at, email=...)])
client.audiences.add_users(segment_id, [AudienceUser(email="someone@example.com")])
client.organizations.posts(organization_id)
client.events.by_organizer(organization_id)
```

Every error is a subclass of `linkedin_marketing.LinkedInError`. Each carries
a `context` dict of the values that caused it, ready for structured logging:
`AuthError`, `RateLimitError` (with `retry_after`), `ApiError`, `NotAllowedError`,
`WriteRefusedError` (with a `reason` code), `ModuleDisabledError` and `ConfigError`.

## Modules and status

| Module | LinkedIn product | Scopes (read / write) | Status | Covers |
|---|---|---|---|---|
| `ads` | Advertising API | `r_ads` / `rw_ads` | **live-tested** | ad accounts, account users, campaign groups, campaigns, creatives, targeting facets/typeahead/similar/resolve, audience counts, ad previews, budget pricing, supply forecasts; pause/resume and daily budget |
| `reporting` | Advertising API | `r_ads_reporting` | **live-tested** | `adAnalytics` (analytics and statistics finders), demographics with names, derived CTR/CPC/CPM/CPL |
| `ad_library` | LinkedIn Ad Library | none beyond a member token | **live-tested** | keyword, advertiser, payer, country and date search with capped paging |
| `leads` | Lead Sync API | `r_marketing_leadgen_automation` | experimental | lead forms, lead form responses |
| `conversions` | Conversions API | `r_ads` / `rw_conversions` | experimental | conversion rules; create rule; send events (SHA-256 hashed emails, dedup `event_id`) |
| `audiences` | Matched Audiences | `r_ads` / `rw_dmp_segments` | experimental | list/create DMP segments; add or remove contacts and companies |
| `organizations` | Community Management API | `r_organization_social`, `r_organization_admin` / `w_organization_social`, `rw_organization_admin` | experimental | pages you administer, posts, social metadata, follower/page/share statistics, follower count; create a post |
| `events` | Events Management API | `r_events` | experimental | events by organizer (read-only) |

**Live-tested** means the requests were verified against the real API on
LinkedIn-Version `202610` (October 2026). **Experimental** means they were built
from the official docs at [learn.microsoft.com/linkedin/marketing](https://learn.microsoft.com/linkedin/marketing)
and LinkedIn's official Postman collections, and are unit-tested against mocked
HTTP only. Each resource's docstring cites the doc pages it follows. Reports
from live use are very welcome.

Enable modules with `LINKEDIN_MODULES` (default `ads,reporting,ad_library`, or
`all`). Using a disabled module raises `ModuleDisabledError` before any request is
made, and the MCP server doesn't register tools for disabled modules.

**Deliberately not included:** delete endpoints of any kind, billing,
creating campaigns or creatives (a mistake there spends money on a live
audience, so build those in Campaign Manager), editing or deleting posts,
member-profile data, and any scraping.

## Getting a LinkedIn app and a token

1. Create an app at [linkedin.com/developers/apps](https://www.linkedin.com/developers/apps).
   It must be linked to a LinkedIn Company Page.
2. On the **Products** tab, request each product you need: **Advertising API**,
   **LinkedIn Ad Library**, **Lead Sync API**, **Conversions API**, **Matched Audiences**,
   **Community Management API**, **Events Management API**. Some are approved
   instantly; others go through review.
3. On the **Auth** tab, add the redirect URL `http://localhost:8765/callback`
   (or your own; see `--redirect-uri`).
4. Your LinkedIn member needs a role on each ad account (Viewer to read, Campaign
   Manager or higher to write) and on each Company Page you use.

**Quick test token:** LinkedIn's [OAuth Token Generator](https://www.linkedin.com/developers/tools/oauth)
issues an access token in the browser. Set it as `LINKEDIN_ACCESS_TOKEN` (it
lasts about 60 days).

**Refresh token (recommended):** run the bundled CLI. It requests the scopes
of the modules you name, waits for LinkedIn's redirect on localhost, checks
`state`, and prints a refresh token once (it lasts about a year):

```bash
export LINKEDIN_CLIENT_ID=... LINKEDIN_CLIENT_SECRET=...
uvx --from "linkedin-marketing @ git+https://github.com/BenA-SA/linkedin-marketing" \
    linkedin-marketing-token --modules ads,reporting,ad_library
# --scopes "r_ads r_ads_reporting"   explicit scopes instead of module presets
# --read-only-scopes                 only the modules' read scopes
# --paste                            no listener; paste the redirected URL instead
```

Store the token in your secret manager as `LINKEDIN_REFRESH_TOKEN`. LinkedIn
may revoke a member's older tokens for an app when a new consent asks for a
different scope set, so mint one token that covers every module you use.
Programmatic refresh tokens must be enabled for your app; if LinkedIn doesn't
issue one, use the access token instead.

## Configuration

Configuration is read from environment variables only. You can also pass
`Settings(...)` to `LinkedInClient` directly.

| Variable | Default | Meaning |
|---|---|---|
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REFRESH_TOKEN` | none | refresh-token auth (recommended) |
| `LINKEDIN_ACCESS_TOKEN` | none | a ready access token instead |
| `LINKEDIN_API_VERSION` | `202610` | `LinkedIn-Version` header (YYYYMM). Retired versions answer 426. |
| `LINKEDIN_MODULES` | `ads,reporting,ad_library` | comma list, or `all` |
| `LINKEDIN_READ_ONLY` | `true` | refuse every confirmed write; set `false` to allow writes |
| `LINKEDIN_ALLOWED_AD_ACCOUNT_IDS` | none (no restriction) | comma list; everything else is refused, reads included |
| `LINKEDIN_ALLOWED_ORGANIZATION_IDS` | none (no restriction) | comma list for pages |
| `LINKEDIN_MAX_DAILY_BUDGET` | none (budget writes refused) | cap for daily-budget changes, in the campaign currency |
| `LINKEDIN_AUDIT_LOG` | `$XDG_STATE_HOME/linkedin-marketing/writes.jsonl` | JSONL log of confirmed writes |
| `LINKEDIN_MAX_PAGES` | `20` | cap on pages any auto-paging call fetches |
| `LINKEDIN_RETRY_AFTER_MAX_SECONDS` | `10` | on 429, wait and retry once if `Retry-After` is at most this; `0` disables |
| `LINKEDIN_REDIRECT_URI` | `http://localhost:8765/callback` | used by `linkedin-marketing-token` |

Tokens and the client secret are never logged, never included in errors, and
never shown in dry runs or `repr`.

## Safety model

Every write goes through one gate, in the library itself, so the safety applies
whether you use Python or MCP.

1. **Dry run by default.** Without `confirm=True`, a write returns the exact
   request it would send (method, URL, headers without the token, JSON body),
   plus the before and after values. Nothing is sent.
2. **Read-only mode is on by default.** With `LINKEDIN_READ_ONLY=true`, even
   `confirm=True` is refused (`WriteRefusedError(reason="read_only_mode")`).
3. **Allow-lists.** When `LINKEDIN_ALLOWED_AD_ACCOUNT_IDS` or
   `LINKEDIN_ALLOWED_ORGANIZATION_IDS` is set, any read or write that names
   another account or page is refused before a request is built. A report can be
   narrowed to campaigns only after a search under that account's own path
   confirms they belong to it.
4. **Narrow writes and caps.** Status changes are limited to ACTIVE and PAUSED,
   so draft, archived or completed campaigns can't be launched or revived.
   Daily budgets must be positive and at most `LINKEDIN_MAX_DAILY_BUDGET`, and
   are refused entirely while no cap is set. Batches are capped at LinkedIn's
   limits.
5. **Audit log.** Each confirmed write appends `attempted`, then `succeeded`
   or `failed`, to a local JSONL file. The log records ids, before/after values
   and counts. It never records request bodies, so hashed emails from Conversions
   or Audiences writes don't end up in it.
6. **Personal data.** Emails are SHA-256 hashed locally (lowercased, whitespace
   removed), so a plain email never leaves your machine.
7. **Never deletes.** There are no delete, billing, create-campaign or
   create-creative operations at all.

## MCP server

The server exposes one tool per library call for each enabled module (31 tools
with every module on). Write tools are annotated as non-read-only, and their
descriptions tell the model to show the dry run and only confirm once the user
approves it. Experimental tools are labelled `[EXPERIMENTAL]`.

Run it with:

```bash
uvx --from "linkedin-marketing[mcp] @ git+https://github.com/BenA-SA/linkedin-marketing" linkedin-marketing-mcp
```

**Claude Code** (`.mcp.json` in your project; keep secrets in the environment,
not in the file):

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["--from", "linkedin-marketing[mcp] @ git+https://github.com/BenA-SA/linkedin-marketing",
               "linkedin-marketing-mcp"],
      "env": {
        "LINKEDIN_CLIENT_ID": "${LINKEDIN_CLIENT_ID}",
        "LINKEDIN_CLIENT_SECRET": "${LINKEDIN_CLIENT_SECRET}",
        "LINKEDIN_REFRESH_TOKEN": "${LINKEDIN_REFRESH_TOKEN}",
        "LINKEDIN_MODULES": "ads,reporting,ad_library",
        "LINKEDIN_ALLOWED_AD_ACCOUNT_IDS": "123456789"
      }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`) and **Cursor**
(`~/.cursor/mcp.json` or `.cursor/mcp.json`) use the same `mcpServers` block. Cursor
expands `${env:LINKEDIN_CLIENT_ID}`-style references; Claude Desktop does not
expand variables, so either launch it from an environment that already has them
set or use a small wrapper script that injects them from your secret manager.
Avoid pasting tokens into config files.

| Module | Tools |
|---|---|
| ads | `li_list_ad_accounts`, `li_list_account_users`, `li_list_campaign_groups`, `li_list_campaigns`, `li_list_creatives`, `li_search_targeting`, `li_list_targeting_facets`, `li_audience_count`, `li_preview_creative`, `li_budget_pricing`, `li_set_campaign_status`\*, `li_set_campaign_group_status`\*, `li_set_campaign_daily_budget`\* |
| reporting | `li_ad_analytics`, `li_ad_demographics` |
| ad_library | `li_search_ad_library` |
| leads | `li_list_lead_forms`, `li_list_leads` |
| conversions | `li_list_conversion_rules`, `li_create_conversion_rule`\*, `li_send_conversion_events`\* |
| audiences | `li_list_audiences`, `li_create_audience`\*, `li_update_audience_contacts`\*, `li_update_audience_companies`\* |
| organizations | `li_list_my_organizations`, `li_list_organization_posts`, `li_post_engagement`, `li_organization_statistics`, `li_create_organization_post`\* |
| events | `li_list_events` |

\* write tool: dry run unless `confirm=True`.

When an allow-list is set, tools default to its first id, so a single-account
setup never needs an `account_id` argument.

## Rate limits

LinkedIn enforces daily per-application and per-member quotas that reset at
midnight UTC. The numbers aren't published per endpoint; your app's
**Analytics** tab in the Developer Portal shows its own limits and usage. See
[Rate limits](https://learn.microsoft.com/linkedin/shared/api-guide/concepts/rate-limits).

- A `429` with a short `Retry-After` (10 seconds or less by default) is retried once.
  Anything longer raises `RateLimitError` with `retry_after`.
- The **Ad Library quota is small.** A new app got roughly a dozen searches a
  day before every call returned `429 TOO_MANY_REQUESTS`. Searches fetch at most
  25 ads per page (LinkedIn's limit) and stop at `LINKEDIN_MAX_PAGES`.
- `adAnalytics` returns at most 15,000 rows per call and is not paged. Split long
  date ranges into several calls.

**Reporting privacy.** Demographic (`MEMBER_*`) values are approximate.
Values with fewer than 3 events are dropped, only the top 100 values per
creative per day are returned, data lags 12–24 hours, and demographic data is
kept for 2 years. `approximateMemberReach` and conversion value aren't
available with these pivots. LinkedIn also adds noise to small daily counts,
so use `time_granularity="ALL"` for totals.

## Why another LinkedIn MCP?

Most "LinkedIn MCP" servers automate a member's own LinkedIn session to scrape
profiles, run searches or send messages. That's a different, unofficial
category, and it risks your account. Several good servers do use the official
Marketing API, but as of October 2026 none of them offered what this project
needed: a reusable Python library, safe-by-default writes, and the products
beyond Ads.

| Project | API | Language | Coverage | Writes | Library use | Licence |
|---|---|---|---|---|---|---|
| **this project** | official Marketing API | Python | Ads, Reporting, Ad Library, Lead Sync, Conversions, Audiences, Community, Events | dry run by default, read-only default, allow-lists, caps, audit log, no deletes | yes (library first) | MIT |
| [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) | browser session (unofficial) | Python | profiles, companies, jobs, messages | n/a | no | Apache-2.0 |
| [cesteral/mcp-open-advertising](https://github.com/cesteral/mcp-open-advertising) (`linkedin-mcp`) | official Marketing API | TypeScript | Ads CRUD, analytics, targeting, forecasts | generic create/update/**delete** | no | Apache-2.0 |
| [danielpopamd/linkedin-ads-mcp](https://github.com/danielpopamd/linkedin-ads-mcp) | official Marketing API | TypeScript | Ads, analytics, demographics, conversions | create/update/**delete** | no | MIT |
| [Walma-Labs/linkedin-ads-mcp](https://github.com/Walma-Labs/linkedin-ads-mcp) | official Marketing API | TypeScript | Ads reporting, demographics, targeting | drafts only | partial (JS) | MIT |
| [flin-agency/flin-linkedin-ads-mcp](https://github.com/flin-agency/flin-linkedin-ads-mcp) | official Marketing API | Python | Ads, insights, Ad Library | none (read-only) | no | MIT |
| [Nuraveda/linkedin-ads-mcp](https://github.com/Nuraveda/linkedin-ads-mcp) | official Marketing API | Python | Ads, analytics | create/status changes | no | MIT |
| [linkedin-developers/linkedin-api-python-client](https://github.com/linkedin-developers/linkedin-api-python-client) | official, generic Rest.li | Python | any endpoint, untyped | no safety layer | yes | proprietary LinkedIn licence (no redistribution); beta, last release 2023 |

If one of those fits you better, use it. Contributions that move good ideas in
either direction are welcome.

## Known gaps in LinkedIn's docs

Choices made where the docs are silent or contradict themselves (all are
pinned by tests, so a correction is a one-line change):

- **Ad Library** isn't documented on Microsoft Learn. Parameters and response
  fields come from live calls (`keyword`, `advertiser`, `payerName`,
  `countries=List(urn%3Ali%3Acountry%3Agb)`, `count` ≤ 25). `dateRange` is
  unverified.
- **Lead Sync:** `submittedAtTimeRange=(start:ms,end:ms)` follows LinkedIn's
  Postman collection. Answers are read from `answerDetails`, falling back to the
  schema table's `accepted`.
- **Matched Audiences:** the docs use three spellings of the user fields; this
  library sends the schema table's `firstName`/`lastName`/`title`/`company`/`country`.
- **Conversions:** the docs say a batch returns 201, while Postman's test
  expects 200 with per-element statuses. Both count as success here.
- **Organizations:** the page-statistics docs flip which end of the time range is
  inclusive; social metadata lists `r_organization_social_feed` scopes.

## Roadmap

- Publish to PyPI (needs the maintainer's PyPI account; until then, install from Git).
- Live-verify the experimental modules and promote them.
- Async client.

## Contributing, security, licence

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). The
licence is [MIT](LICENSE), © 2026 Ben Atkinson.
