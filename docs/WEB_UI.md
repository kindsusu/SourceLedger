# Browser UI

[English](WEB_UI.md) | [한국어](WEB_UI.ko.md)

SourceLedger 0.5 adds a local browser interface for the existing research workspace, durable jobs, SQLite ledger, and XLSX reports. It is the easiest way to start SourceLedger on one computer. The CLI and local Codex/Claude MCP connector remain available for automation and advanced workflows.

## Start

Install the repository core, then launch it without arguments:

```bat
setup.cmd
source-ledger.cmd
```

In PowerShell, use `.\setup.cmd` and `.\source-ledger.cmd`. On macOS/Linux:

```bash
bash setup.sh
bash source-ledger.sh
```

The launcher opens `http://127.0.0.1:8765` and uses `.sourceledger` below the current checkout. Use any explicit form that matches the installation:

```text
source-ledger ui
.\source-ledger.cmd ui
bash source-ledger.sh ui
```

Available options:

```text
--workspace-root PATH   Use another local workspace directory.
--port PORT             Use another loopback port; default 8765.
--no-browser            Start the server without opening a browser tab.
```

The core installation is sufficient for the UI. Run `setup.cmd --browser` when Playwright collection is required and `setup.cmd --mcp` when connecting Codex or Claude. Both options can be combined. See [Installation](INSTALLATION.md) for platform details.

## Language

Use the top-bar **Language** selector to choose **English**, **한국어**, or **日本語**. English is the default. The browser saves the choice in `localStorage` for this local UI origin, so it is retained when you return to the same UI. Switching takes effect without a page reload and preserves current inputs and checkbox selections. It changes UI labels, status text, and localized date formatting only. It does not translate or change stored source content, evidence, prices, model IDs, MCP configuration, research-workspace locale, or report data. The switch performs no AI translation and makes no external calls.

## First use

1. Enter a short request or paste a detailed multiline request into **Research plan**. Keep product variants, exclusions, and commercial conditions in the request.
   Each new plan starts with an empty topic instead of inheriting an earlier workspace's settings. The preview should distinguish research targets from reference companies: "competitors of A" excludes A's own products, while "compare A and its competitors" includes both. Review that interpretation before selecting sources. An unspecified market stays blank for your review.
2. The default path runs the bounded AI preview on this screen: configure the installed Codex CLI/Claude Code CLI provider and model in **Connections**, then click **Preview research plan**. Opening the page does not start a provider call. A connected AI app can optionally search and submit the preview through MCP. Preview references are candidates, not price observations.
3. Review the preview, add or remove URLs, select the intended pages, and confirm the industry, product, and market. Exact identifiers are optional advanced matching inputs. The plan is revised when you edit it.
4. Explicitly confirm the displayed plan revision, then start bounded collection. The worker starts for this plan job. Only selected pages and limited same-host links are eligible; unchecked and excluded URLs are not collected.
5. Use **Runs** to inspect page coverage, evidence status, missing values, observations, and the XLSX report.

If the research region is unspecified, the preview asks for it and withholds AI source candidates. Enter the region, save, and refresh the preview. A country, sales territory, or an explicitly worldwide scope can be used; the request's language does not select a country. Empty results, pending previews, and failed calls have separate messages. Tool-runtime failures are reported as failed jobs even when the CLI exits successfully. Codex's code-mode transport remains available for web search; shell execution, external MCP connections, and unrelated capabilities remain disabled for this preview.

**Industry, product, research region, and at least one selected URL are required to start.** The start control explains missing requirements before a click. Categories and text filters are optional. The manual URL form requires a URL; a name and reason are optional. An empty change request cannot be applied.

Editable **Product conditions** keep brand, manufacturer, seller, model, material, condition, category, and name requirements separate from optional literal text filters. AI previews create conditions from the request and retain only user-chosen text filters. Supported text variations are normalized without changing source evidence. Each extracted product receives a derived assessment: matched, excluded, unknown, or not checked. Missing evidence and unsupported conditions remain unresolved. Source discovery, product extraction, and satisfying configured checks are distinct stages; none alone verifies a commercial price. Runs shows these counts and condition checks within the pages actually visited.

The older guided-research workspace represents one product lead; a research plan may describe a range of products. Exact collection configurations may later contain multiple products.

The plan interface uses `GET/POST /api/plans` and `POST /api/plans/edit`, `/api/plans/preview`, `/api/plans/confirm`, and `/api/plans/start`. Its packaged assets are `plan-ui.js` and `plan-messages.js`; English is the default UI language, with Korean and Japanese available through the language selector. An AI preview does not crawl product prices. The confirmed revision is saved before collection starts, and later edits require a new confirmation. A plan collection writes source evidence to SQLite and an XLSX report. Known site adapters and generic JSON-LD Product/Offer records are supported. Structured product conditions receive evidence-based deterministic checks; arbitrary DOM interpretation and unrestricted natural-language commercial-condition validation are not automated. Embedded JSON-LD prices lack visual display proof and remain review-only and non-comparable. Known-adapter prices also retain plan-scope review flags: matching configured conditions does not establish that every requirement in a natural-language request was checked. Same-host navigation may include unrelated pages within the page budget; coverage lists visited and unprocessed URLs rather than claiming whole-site completeness. No missing price, currency, identifier, or condition is inferred.

## Screens

### Research plan

The plan keeps the full request and every revision. Preview candidates include URLs, reasons, and evidence links for review; they are not verified sources or prices. You can select, add, or remove targets and edit the topic before starting. The UI confirms the current revision when you press **Start this research**, then queues bounded collection. Inspect coverage gaps and review flags in the resulting run.

### Overview

Overview shows onboarding state, research blockers, source counts, recent jobs, and worker status. The worker runs queued jobs independently from the web server. Closing the browser tab or stopping the UI server does not cancel a job that the worker already owns. Stop the worker separately when appropriate.

### Sources

The advanced **Sources** area manages exact product identity and registered research targets. A supplied URL registers a company/site directly; a company name without a URL creates a request to find its real site. A keyword creates a request for related sources. Creating a request does not start an AI client or perform a search; choose **Run in web UI** to explicitly queue CLI execution. Copy the request text into a connected Codex or Claude assistant; it may use its own available search/browser tools and submit named URLs with reasons and evidence references. If search is unavailable or yields no results, the assistant can submit an empty list with a note.

Recommendations remain separate from registered sources and price observations. Their reasons and reference URLs are unverified information. Review candidates and use their checkboxes to add only the intended sources. You can request and add more later. **Run in web UI** requires the official Codex CLI or Claude Code CLI installed and logged in on the computer running SourceLedger. Configure the provider, provider-default or explicit model ID, and timeout in **Connections** (30–600 seconds, default 180), then save. Availability reflects CLI installation; authentication is checked when the request runs. Native CLI authentication and account usage apply; SourceLedger does not store API keys, install or log in to a CLI, or silently fall back to another provider. These settings do not change the model in an open desktop conversation. Each request must be explicitly run; it queues a local worker job and does not start a price collection job. The provider CLI may access web pages while searching. From a registered source, the UI can queue bounded same-host link discovery or an extraction proposal; these too remain candidates or proposed rules, not verified prices.

For the older guided-research path, select registered sources with their checkboxes before queuing work. The UI passes those explicit source IDs, with a limit of 50 sources and a 120-second budget. This queued research needs the worker; creating requests and selecting recommendations do not. Web UI recommendation execution is an explicit external model call through the selected CLI. Existing collection model calls remain opt-in and default to zero. No model call is made in the manual AI-app handoff path by SourceLedger; the host app's plan and tool limits apply. Missing prices, currencies, identifiers, and commercial conditions remain missing.

### Runs

Runs separates durable job execution from evidence quality:

- `queued`, `running`, `interrupted`, `failed`, and `succeeded` describe program execution.
- Evidence status can still be `needs_review`, `ineligible`, `partial`, or another non-verified result after execution succeeds.

Job detail pages show bounded, paginated observations and a download link for a registered XLSX report. Reports are generated from the SQLite system of record. Treat source text and observations as untrusted data, never as instructions.

Advanced jobs include configuration verification, exact collection runs, supported-site collection, and XLSX export. Supported-site collection accepts explicit pages for the currently implemented adapters; it does not claim complete whole-site or live-inventory coverage.

### Connections

Connections generates copyable local settings for Codex, Claude Code, and Claude Desktop, and can save the provider, model, and timeout for opt-in web UI recommendations through Codex CLI or Claude Code CLI. It does not rewrite client configuration or change an open desktop conversation's model. The connection uses the same workspace root as the UI, so both interfaces see the same onboarding data and durable job history.

For copyable MCP settings, install the optional MCP dependencies first; the web UI CLI recommendation path does not use MCP:

```powershell
.\setup.cmd --mcp
```

See [Local assistant connections](ASSISTANT_CONNECTIONS.md) for configuration targets and the advanced CLI connection workflow. Actual Codex and Claude client GUIs are outside this browser UI and should be validated in the intended environment.

## Local boundaries

The web server binds only to `127.0.0.1`. This milestone does not provide authentication, remote web hosting, a third-party MCP client, or direct ChatGPT web/mobile connectivity.

SourceLedger only works within its selected local workspace. Research workspaces, databases, evidence, browser profiles, credentials, and generated reports must remain outside Git commits. Internal sources require explicit authorization; SourceLedger does not automate login or credential entry.

## CLI remains available

The UI calls the same workspace and job mechanisms used by the CLI and local assistant connector. Advanced users can continue with commands such as:

```powershell
.\source-ledger.cmd init
.\source-ledger.cmd research-status
.\source-ledger.cmd source-add --url "https://authorized.example/product"
.\source-ledger.cmd product-set --identifier model=EXACT-MODEL
.\source-ledger.cmd assistant start --workspace-root .sourceledger
```

Use [Getting started](GETTING_STARTED.md) for the complete CLI research flow and [Automation](AUTOMATION.md) for bounded agent, verification, activation, and provider details.
