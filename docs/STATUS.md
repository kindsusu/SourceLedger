# Implementation status

[English](STATUS.md) | [한국어](IMPLEMENTATION_STATUS.md)

SourceLedger currently provides a working collection engine, not an unrestricted autonomous research agent.

## Available

- Explicit product identifiers, source URLs/files, and extraction rules.
- HTTP and local files, with optional Playwright browser actions and a Crawl4AI adapter.
- HTML/JSON-LD, JSON, CSV, XLSX, and explicitly configured text-PDF extraction.
- Evidence files, source locations, collection timestamps, and SHA-256 hashes.
- Strict price, currency, product, specification, and commercial-condition validation.
- SQLite run history, resumable tasks, and six-sheet XLSX reports. Missing prices remain blank.
- A local MCP server and an independently started queue worker.
- Bounded link and sitemap discovery within configured source domains.

## Verified baseline

The Windows test suite passes 44 tests. It includes real local Chrome actions, evidence capture, an actual stdio MCP session, collection continuing after MCP disconnect, and report generation. Six demo workbook sheets were visually inspected. Demo values are synthetic, not market prices.

## Still required for unattended research

- First-run industry, product, and market setup.
- Search-provider integration, source discovery, and persistent candidate review.
- Source onboarding, extraction-rule generation, independent validation, and change recovery.
- Scheduling, bounded budgets, operational alerts, and service deployment.
- Representative real-site evaluation and client-specific Claude/ChatGPT connection testing.

Crawl4AI is an optional, unverified runtime in this environment. Agent-Reach informed routing and diagnostics; it is not directly integrated. ego-lite was reviewed but depends on a macOS application. UCP, commerce-agents, and insane-search are design references rather than integrated runtimes. No paid Ultimate Web Scraper MCP connection has been made.

Primary delivery remains SQLite plus evidence and XLSX. CSV export, ERP integration, and analytical/simulation functions are not part of the current implementation.

Earlier design notes in this repository are retained as historical planning documents; implemented behavior is described here and in the README.
