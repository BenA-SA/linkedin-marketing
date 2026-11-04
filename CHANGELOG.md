# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Conversions: campaign association, without which LinkedIn attributes no
  conversions. `create_rule` takes `auto_association` (`ALL_CAMPAIGNS` or
  `OBJECTIVE_BASED`, sent as `?autoAssociationType=`) and `attribution_type`,
  and its dry run warns when no campaigns will be associated.
  `associate_campaigns` (`PUT /campaignConversions/(campaign:..,conversion:..)`,
  or one `BATCH_UPDATE` for several) checks every campaign belongs to the
  account, skips existing associations, and is a dry run by default.
  `campaign_conversions` reads existing associations (`q=campaigns`).
- Conversions: `create_rules` creates up to 100 rules in one `BATCH_CREATE`
  from `ConversionRuleSpec` values.
- Conversions: `rules(..., include_shared=True)` adds
  `conversionOwnershipTypes=List(OWNED,SHARED)`; `ConversionRule` gains
  `ownership_type` and `account_id`.
- Conversion events accept `lead` (a `urn:li:leadGenFormResponse:` URN, so Lead
  Gen Form leads match), `external_id`, `ip_address` (`PLAINTEXT_IP_ADDRESS`,
  IPv4 only) and `hash_names` (`hashedFirstName`/`hashedLastName`, 202609+).
  `userIds` is sent as `[]` when the member is identified only by those.
- `hash_name` helper and `CampaignNotInAccountError`.
- MCP tools `li_create_conversion_rules`, `li_list_campaign_conversions` and
  `li_associate_campaigns_to_conversion`; `li_create_conversion_rule` takes
  windows, attribution type and auto-association; `li_list_conversion_rules`
  takes `include_shared` (34 tools with every module on).

### Changed
- Conversion rules now always send `attributionType` (LinkedIn's default,
  `LAST_TOUCH_BY_CAMPAIGN`, unless you choose otherwise).
- Conversion rule input is validated against the docs before any request:
  attribution windows per rule type (180 and 365 days only for the long-cycle
  types), `MARKETING_QUALIFIED_LEAD`/`SALES_QUALIFIED_LEAD` only on
  LinkedIn-Version 202608 or later (`reason="version_too_old"`), and a non-blank
  name.

### Fixed
- Conversion rule types now match the Conversions API schema: added the
  missing types (for example `SCHEDULE`, `SUBSCRIBE`, `START_TRIAL`,
  `PHONE_CALL`) and removed the undocumented `APPLICATION_SUBMITTED` and
  `BOOKED_APPOINTMENT` (use `SUBMIT_APPLICATION` and `BOOK_APPOINTMENT`).
- `RateLimitError` no longer claims every 429 is a daily quota. It keeps what
  LinkedIn sent about the limit: `period` (parsed from the body or headers when
  present), `api_message`, `service_error_code`, the limit-related headers plus
  the `x-li-uuid` request id, and the body. The message, which is what an MCP
  client sees, is built from those values.
- The Ad Library search returns the ads already read when a 429 lands after the
  first page, with `stopped: "rate_limited"` and the limit details, instead of
  discarding them.

## [0.1.0] - 2026-10-31

### Added
- Typed Python client for the LinkedIn Marketing API (LinkedIn-Version 202610),
  organised as switchable modules: `ads`, `reporting`, `ad_library`
  (live-tested); `leads`, `conversions`, `audiences`, `organizations`, `events`
  (experimental).
- Refresh-token and access-token auth, Rest.li 2.0 encoding, cursor and offset
  paging with a page cap, one bounded `Retry-After` retry, and a typed error
  hierarchy.
- Safety layer: dry run by default, read-only mode on by default, ad account and
  organization allow-lists, a daily-budget cap, a JSONL audit log, and local
  SHA-256 hashing of emails. No delete or billing operations.
- Optional MCP server (`linkedin-marketing-mcp`, `[mcp]` extra) supporting MCP
  SDK 1.x and 2.x, with 31 tools.
- `linkedin-marketing-token` CLI with a localhost OAuth callback and per-module
  scope presets.
