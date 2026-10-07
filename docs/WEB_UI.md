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

## First use

1. Open **Overview** and enter the industry, product, and market. SourceLedger does not ask for an analysis purpose.
2. Open **Sources** and register a company/site URL directly, or enter only a company name or a keyword to prepare an AI recommendation request. Registration does not contact a source.
3. For a recommendation request, either copy its prompt into a connected AI app and submit candidates through MCP, or choose **Run in web UI** to queue it for the configured local Codex CLI or Claude Code CLI. Review candidates and tick only the sources you want to add.
4. Before guided research, enter exact product identifiers such as a model, SKU, or catalog number. No identifier is inferred from the product name. Recommendation requests and source selection do not require these identifiers.
5. Select registered sources, start the independent worker, and queue a guided research run from **Runs**.
6. Use **Runs** to inspect execution status, evidence status, observations, missing values, and XLSX output.

The first workspace represents one research product lead. Exact collection configurations may later contain multiple products.

## Screens

### Overview

Overview shows onboarding state, research blockers, source counts, recent jobs, and worker status. The worker runs queued jobs independently from the web server. Closing the browser tab or stopping the UI server does not cancel a job that the worker already owns. Stop the worker separately when appropriate.

### Sources

Sources manages exact product identity and registered research targets. A supplied URL registers a company/site directly; a company name without a URL creates a request to find its real site. A keyword creates a request for related sources. Creating a request does not start an AI client or perform a search; choose **Run in web UI** to explicitly queue CLI execution. Copy the request text into a connected Codex or Claude assistant; it may use its own available search/browser tools and submit named URLs with reasons and evidence references. If search is unavailable or yields no results, the assistant can submit an empty list with a note.

Recommendations remain separate from registered sources and price observations. Their reasons and reference URLs are unverified information. Review candidates and use their checkboxes to add only the intended sources. You can request and add more later. **Run in web UI** requires the official Codex CLI or Claude Code CLI installed and logged in on the computer running SourceLedger. Configure the provider, provider-default or explicit model ID, and timeout in **Connections** (30–600 seconds, default 180), then save. Availability reflects CLI installation; authentication is checked when the request runs. Native CLI authentication and account usage apply; SourceLedger does not store API keys, install or log in to a CLI, or silently fall back to another provider. These settings do not change the model in an open desktop conversation. Each request must be explicitly run; it queues a local worker job and does not start a price collection job. The provider CLI may access web pages while searching. From a registered source, the UI can queue bounded same-host link discovery or an extraction proposal; these too remain candidates or proposed rules, not verified prices.

Select registered sources with their checkboxes before queuing guided research. The UI passes those explicit source IDs, with a limit of 50 sources and a 120-second budget. This queued research needs the worker; creating requests and selecting recommendations do not. Web UI recommendation execution is an explicit external model call through the selected CLI. Existing collection model calls remain opt-in and default to zero. No model call is made in the manual AI-app handoff path by SourceLedger; the host app's plan and tool limits apply. Missing prices, currencies, identifiers, and commercial conditions remain missing.

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
