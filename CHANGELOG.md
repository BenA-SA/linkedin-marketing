# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).

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
