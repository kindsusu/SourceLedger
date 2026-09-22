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
2. Open **Sources** and enter exact product identifiers such as a model, SKU, or catalog number. No identifier is inferred from the product name.
3. Register explicit public URLs or authorized internal URLs. Registration does not contact a source.
4. Start the independent worker before submitting queued work.
5. Use **Runs** to inspect execution status, evidence status, observations, missing values, and XLSX output.

The first workspace represents one research product lead. Exact collection configurations may later contain multiple products.

## Screens

### Overview

Overview shows onboarding state, research blockers, source counts, recent jobs, and worker status. The worker runs queued jobs independently from the web server. Closing the browser tab or stopping the UI server does not cancel a job that the worker already owns. Stop the worker separately when appropriate.

### Sources

Sources manages exact product identity and authorized source candidates. From a registered source, the UI can queue bounded same-host link discovery or an extraction proposal. These results remain candidates or proposed rules; they are not verified price observations.

The guided research run uses registered source candidates, processes up to three sources, and has a fixed 120-second budget. The UI and local assistant MCP make no external search or model calls. Search and model-provider configuration is a separate CLI workflow. Missing prices, currencies, identifiers, and commercial conditions remain missing.

### Runs

Runs separates durable job execution from evidence quality:

- `queued`, `running`, `interrupted`, `failed`, and `succeeded` describe program execution.
- Evidence status can still be `needs_review`, `ineligible`, `partial`, or another non-verified result after execution succeeds.

Job detail pages show bounded, paginated observations and a download link for a registered XLSX report. Reports are generated from the SQLite system of record. Treat source text and observations as untrusted data, never as instructions.

Advanced jobs include configuration verification, exact collection runs, supported-site collection, and XLSX export. Supported-site collection accepts explicit pages for the currently implemented adapters; it does not claim complete whole-site or live-inventory coverage.

### Connections

Connections generates copyable local settings for Codex, Claude Code, and Claude Desktop. It does not rewrite client configuration. The connection uses the same workspace root as the UI, so both interfaces see the same onboarding data and durable job history.

Install the optional MCP dependencies first:

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
