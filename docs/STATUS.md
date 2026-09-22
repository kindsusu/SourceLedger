# Implementation status

[English](STATUS.md) | [한국어](IMPLEMENTATION_STATUS.md)

SourceLedger 0.3 provides evidence-first collection and bounded research execution. It is not an unrestricted autonomous research agent.

This correction update keeps raw/static HTML prices and their field-level evidence, but does not compare them until rendered display evidence is available. Structured files and document records remain eligible under their existing evidence rules. Fallback selects the strongest available evidence within finite attempts; missing commercial terms do not cause indefinite retries. Source Evidence records content, screenshot, and collection-receipt references when captured. Activation checks their hashes and reconstructs the original capture backend before re-extraction. `collect-sites` examples use the `.sourceledger/research.json` workspace created by `init` unless a separate `--workspace` is supplied.

The Windows suite passed **212 tests** on 2026-09-22 (`SOURCELEDGER_HEADLESS=1`, `.venv/Scripts/python.exe -m pytest tests -q`). Tests used local fixtures, local HTTP servers, and mocked responses. The earlier correction-only baseline was 192 tests. This update did not recollect live sites, install or connect insane-search, or connect paid services.

[Portable installation](INSTALLATION.md) now includes Windows and macOS/Linux setup and launch scripts, a repository-local virtual environment, UTF-8 input, optional managed Chromium, and a direct dependency constraints file. A fresh Windows folder containing Korean characters, spaces, and an exclamation mark passed installation and initialization from another working directory. A separately installed wheel passed local verification, activation, SQLite recording, and XLSX generation. The [CI workflow](../.github/workflows/tests.yml) is configured for Windows/macOS/Linux on Python 3.12, Ubuntu on Python 3.11 and 3.13, and isolated wheel installation; see [workflow results](https://github.com/kindsusu/SourceLedger/actions/workflows/tests.yml) for hosted outcomes. Existing evidence workspaces still need path-aware migration; copying them alone is not a supported migration procedure.

The preceding collection-reliability update added supported rental-page adapters,
the `collect-sites` entry point, separate calculator estimates, conditional HTTP
revalidation, clearer access diagnostics, and a conditional Rental Quotes report
sheet. See [collection reliability](COLLECTION_RELIABILITY.md) and
[Milestone 04](MILESTONE_04.md) for its verification and limits. That
2026-09-22 Windows baseline passed **172 tests**.

## Implemented

- **Research workspace:** `init` creates an English or Korean first-run workspace for one lead product: industry, product, and market. It stores explicit identifiers, authorized same-host source candidates, readiness, and next actions. It does not ask for an analysis-purpose field or secret values.
- **Bounded candidate discovery:** a saved candidate seed can discover same-host links within a limit. HTTP is tried first and the configured browser fallback is used for a blocked or unavailable page. Results remain candidates and never become prices, active sources, or extraction rules automatically.
- **Draft and activation controls:** an explicit-identifier workspace can produce a draft configuration. `verify` checks the exact configuration and retained evidence; optional known samples add independent product/price checks. `activate` accepts only the exact verified configuration and evidence. It rejects changed configuration, missing or altered evidence, stale evidence, missing assigned products, and mismatched known samples. Receipts are local audit records, not signed attestations.
- **Existing collection engine:** explicit product identifiers, source URLs/files, extraction rules, HTTP/file collection, optional Playwright browser actions, and optional Crawl4AI adapter.
- **Evidence and validation:** retained source bytes, locators, timestamps, SHA-256 hashes, and strict product, price, currency, specification, and commercial-condition validation. Missing prices remain blank.
- **Display proof and artifacts:** raw/static HTML field evidence is retained as unconfirmed until a rendered capture establishes display state. CSV/XLSX/JSON records and document text use record evidence instead. Source Evidence can link retained content, screenshots, and collection receipts; conflicting amounts remain distinct records for review.
- **Storage and reporting:** SQLite run history, resumable tasks, evidence files, six standard XLSX sheets, and an additional Rental Quotes sheet when rental observations exist. The default workbook labels are English; Korean onboarding and documentation are available.
- **Local MCP operation:** a local MCP server registers configured jobs for an independently started worker. Bounded discovery and research workspaces are separate from active `run` configuration; running a configured collection does not require activation.
- **Keyword search:** opt-in SearXNG JSON API, one bounded request, explicit provider settings, URL filtering, and candidate provenance. No configured provider means zero search calls.
- **Rule proposals:** structured-data and semantic HTML extraction proposals, optional local Ollama selector-only output, DOM checks, exact identifiers, retained evidence and previews. Missing conditions remain unobserved.
- **Bounded controller:** `agent` checkpoints source tasks, preserves scope and budgets across resume, proposes each source, and performs one fresh combined collection into the shared SQLite ledger and XLSX. Optional automatic activation requires known samples for every selected source/product. Partial sources remain visible.

## Validation scope

Version 0.3 passed **110 tests** on Windows (2026-09-17). The added coverage includes the SearXNG-to-agent flow with controlled HTTP responses, local HTTP collection through the CLI, rule and model-response validation, saved budgets, pause/resume, modified-draft rejection, and sample-gated activation. No live SearXNG/Ollama server or model was installed or invoked.

The 0.2 integrated Windows baseline passed **74 tests** (`python -m pytest tests -q`). This includes a real local HTTP onboarding-to-collection flow, actual local browser actions, MCP disconnection recovery, and XLSX checks. Browser fallback after a simulated HTTP block is covered by a controlled test; real-site block recovery is not established.

The earlier collection baseline recorded **44 passing tests** on Windows/Python 3.12, including local Chrome actions, evidence capture, stdio MCP, worker continuation after MCP disconnect, and XLSX rendering. That number is a historical baseline, not a claim about the current integrated suite.

Current integration coverage includes first-run workspaces, English/Korean input handling, saved candidates, bounded same-host discovery with browser fallback, draft generation, verification receipts, optional known samples, and activation rejection for changed or missing evidence. Known samples are optional: without them, `verify` validates configuration and evidence integrity but does not claim an independent price-ground-truth check.

Demo values are synthetic and are not market prices. Representative live-site accuracy, access success, and browser recovery must be measured on the intended sites.

## Not implemented or not verified

- Live SearXNG/Ollama server and actual model validation; current provider tests use controlled HTTP responses
- Arbitrary browser action planning, recipe repair, and unattended self-repair of existing rules
- Scheduling, service deployment, alerts, and ongoing unattended operations
- Claude or ChatGPT client UI connection verification and remote artifact delivery
- Representative live-site collection evaluation
- OCR for scanned PDFs, ERP integration, CSV export, and analysis/simulation features

Crawl4AI is optional and unverified against live sites in this environment. Agent-Reach informed diagnostics and routing design, but is not a direct runtime dependency. ego-lite is not directly integrated on Windows. UCP, commerce-agents, insane-search, and paid Ultimate Web Scraper MCP remain design references or unconnected services.

See the [automation guide](AUTOMATION.md) for 0.3 commands and provider prerequisites. The time budget is cooperative with bounded external request timeouts; it is not a process-kill guarantee. Ollama must run with cloud features disabled; a loopback endpoint alone does not attest to server behavior.
