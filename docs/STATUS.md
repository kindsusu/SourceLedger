# Implementation status

[English](STATUS.md) | [한국어](IMPLEMENTATION_STATUS.md)

SourceLedger provides an evidence-first collection engine and a bounded research-onboarding path. It is not an unrestricted autonomous research agent.

## Implemented

- **Research workspace:** `init` creates an English or Korean first-run workspace for one lead product: industry, product, and market. It stores explicit identifiers, authorized same-host source candidates, readiness, and next actions. It does not ask for an analysis-purpose field or secret values.
- **Bounded candidate discovery:** a saved candidate seed can discover same-host links within a limit. HTTP is tried first and the configured browser fallback is used for a blocked or unavailable page. Results remain candidates and never become prices, active sources, or extraction rules automatically.
- **Draft and activation controls:** an explicit-identifier workspace can produce a draft configuration. `verify` checks the exact configuration and retained evidence; optional known samples add independent product/price checks. `activate` accepts only the exact verified configuration and evidence. It rejects changed configuration, missing or altered evidence, stale evidence, missing assigned products, and mismatched known samples. Receipts are local audit records, not signed attestations.
- **Existing collection engine:** explicit product identifiers, source URLs/files, extraction rules, HTTP/file collection, optional Playwright browser actions, and optional Crawl4AI adapter.
- **Evidence and validation:** retained source bytes, locators, timestamps, SHA-256 hashes, and strict product, price, currency, specification, and commercial-condition validation. Missing prices remain blank.
- **Storage and reporting:** SQLite run history, resumable tasks, evidence files, and six-sheet XLSX reports. The default workbook labels are English; Korean onboarding and documentation are available.
- **Local MCP operation:** a local MCP server registers configured jobs for an independently started worker. Bounded discovery and research workspaces are separate from active `run` configuration; running a configured collection does not require activation.

## Validation scope

On 2026-09-17, the integrated Windows suite passed **74 tests** (`python -m pytest tests -q`). This includes a real local HTTP onboarding-to-collection flow, actual local browser actions, MCP disconnection recovery, and XLSX checks. Browser fallback after a simulated HTTP block is covered by a controlled test; real-site block recovery is not established.

The earlier collection baseline recorded **44 passing tests** on Windows/Python 3.12, including local Chrome actions, evidence capture, stdio MCP, worker continuation after MCP disconnect, and XLSX rendering. That number is a historical baseline, not a claim about the current integrated suite.

Current integration coverage includes first-run workspaces, English/Korean input handling, saved candidates, bounded same-host discovery with browser fallback, draft generation, verification receipts, optional known samples, and activation rejection for changed or missing evidence. Known samples are optional: without them, `verify` validates configuration and evidence integrity but does not claim an independent price-ground-truth check.

Demo values are synthetic and are not market prices. Representative live-site accuracy, access success, and browser recovery must be measured on the intended sites.

## Not implemented or not verified

- Search-provider integration, arbitrary web search, and automatic source discovery beyond an explicit same-host seed
- AI-generated extraction rules, recipe proposals, self-repair, and an agent planning loop
- Scheduling, service deployment, alerts, and ongoing unattended operations
- Claude or ChatGPT client UI connection verification and remote artifact delivery
- Representative live-site collection evaluation
- OCR for scanned PDFs, ERP integration, CSV export, and analysis/simulation features

Crawl4AI is optional and unverified against live sites in this environment. Agent-Reach informed diagnostics and routing design, but is not a direct runtime dependency. ego-lite is not directly integrated on Windows. UCP, commerce-agents, insane-search, and paid Ultimate Web Scraper MCP remain design references or unconnected services.
