# SourceLedger runtime architecture

[한국어](ARCHITECTURE.ko.md) · [Diagram source](architecture/sourceledger.architecture.json) · [Interactive diagram](architecture/sourceledger.html) · [PNG preview](architecture/sourceledger.png) · [Provenance](architecture/PROVENANCE.md) · [Verification](architecture/verification.json)

SourceLedger keeps evidence and collection results separate from discovery and rule proposals. SQLite is the system of record; the XLSX workbook is a human-readable report generated from one run. This document describes implemented local paths. It does not claim a live external-provider evaluation.

## Read the diagram

Open `docs/architecture/sourceledger.html` locally in a modern browser for the interactive diagram. GitHub renders the JSON source but does not execute the standalone HTML. Use the PNG preview for a static repository view or download/open the HTML locally for theme switching, pan/zoom, search, focus, relationship tracing, and export controls.

The authored diagram is in English so its component labels match the code and command surface. This guide has Korean parity.

## Runtime paths

The main path is bounded research through the manual CLI. A separate MCP/worker card shows the alternate route for an already configured collection:

1. **Manual CLI.** The operator initializes a research workspace, supplies exact identifiers and source candidates, and can run `search`, `propose`, `agent`, `verify`, `activate`, or a configured collection. The command dispatcher is in [`su_crawler/cli.py`](../su_crawler/cli.py).
2. **Local MCP.** A local MCP server accepts only a fixed collection configuration. `start_collection` writes a local dispatch receipt; an independently started worker consumes it and calls the same collection pipeline. See [`su_crawler/mcp_server.py`](../su_crawler/mcp_server.py) and [`su_crawler/worker.py`](../su_crawler/worker.py).

The bounded agent persists its inputs, limits, and source-task checkpoints. It selects web sources only: it may make one configured SearXNG request before selection, then creates review-only source-rule proposals. After those proposal steps, it performs one fresh combined verification collection. The controller is [`su_crawler/agent.py`](../su_crawler/agent.py); the opt-in search path is [`su_crawler/search.py`](../su_crawler/search.py), and proposals are in [`su_crawler/proposals.py`](../su_crawler/proposals.py).

`verify` executes the exact configuration, reads the completed run from SQLite, checks retained evidence and current comparable observations, and writes a local receipt. Optional known samples add an independent product/price comparison. A manual configured `run` does not require activation. `activate` rechecks the receipt, configuration, samples, and evidence before writing a standalone active configuration; automatic agent activation requires known samples for every selected source/product. A receipt is a local audit record, not a signed attestation. See [`su_crawler/activation.py`](../su_crawler/activation.py) and [`su_crawler/agent.py`](../su_crawler/agent.py).

The collection pipeline performs bounded, resumable source/product tasks, saves evidence, extracts and validates observations, records attempts and observations in SQLite, and writes an XLSX report. Its shared configured capability accepts authorized web pages or local files; the agent does not automatically discover internal files. The implementation is [`su_crawler/pipeline.py`](../su_crawler/pipeline.py), [`su_crawler/storage.py`](../su_crawler/storage.py), and [`su_crawler/report.py`](../su_crawler/report.py).

## Scope and provider status

- SearXNG search is opt-in. Without a provider configuration, it makes zero search requests; returned snippets are candidate provenance, never prices.
- Selector proposals use deterministic rules by default. A configured local Ollama endpoint may propose selectors only, and its output still requires fresh source verification.
- HTTP collection and configured Playwright fallback are implemented local collection paths.
- Live SearXNG servers and Ollama models have not been validated in this project. Crawl4AI live-site use is optional and unvalidated. Paid Ultimate Web Scraper MCP and other cited research tools are not connected runtime services.

See [status](STATUS.md) and [automation](AUTOMATION.md) for the tested scope and provider prerequisites.

## Diagram source and rebuild

The diagram assets live together:

| Asset | Purpose |
| --- | --- |
| [`sourceledger.architecture.json`](architecture/sourceledger.architecture.json) | Typed, reviewable Archify source of truth |
| [`sourceledger.html`](architecture/sourceledger.html) | Self-contained interactive HTML output |
| [`sourceledger.png`](architecture/sourceledger.png) | Static preview for repository browsers |
| [`PROVENANCE.md`](architecture/PROVENANCE.md) and [`verification.json`](architecture/verification.json) | Source/version record and generated validation evidence |

Archify is a local Codex skill, not a SourceLedger application dependency. This workstation has Archify `v2.17.0-dev.1` from `tt-a1i/archify` at commit `72c750bb070d95171dbb2244e5b62b1b7da69c12`. A repository clone does not install that skill for contributors; each contributor must install it separately through their Codex skill setup or use an existing local installation. In a Codex environment, the bundled installer can reproduce this pinned source:

```powershell
$archifyHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$installer = Join-Path $archifyHome "skills\.system\skill-installer\scripts\install-skill-from-github.py"
python $installer --repo tt-a1i/archify --path archify --ref 72c750bb070d95171dbb2244e5b62b1b7da69c12
```

Archify works as a documentation build path: an agent inspects code evidence, authors a typed JSON diagram, runs the validators, and deterministically delivers self-contained HTML/SVG. A reviewer or browser check then examines the delivered artifact. This does not automatically monitor SourceLedger at runtime, and layout validation does not prove that the authored topology is true; maintainers must keep the JSON aligned with the code.

Basic deterministic rendering uses Node.js 18 or newer and the installed skill package; it does not require an npm install or an MCP/paid service. On this workstation, `doctor` completed successfully with Node.js `v24.18.0`; that diagnoses the local skill environment, not diagram content. Asking an LLM agent to analyse or revise a diagram is separate work and can consume that agent's model tokens. Archify's optional browser check is also separate from deterministic validation and requires a browser-capable environment.

In PowerShell, set the path to the installed CLI, then diagnose, validate, and deliver from the frozen JSON source:

```powershell
$archifyHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$archifyCli = Join-Path $archifyHome "skills\archify\bin\archify.mjs"
node $archifyCli doctor
node $archifyCli validate architecture docs/architecture/sourceledger.architecture.json --quality showcase --json
node $archifyCli deliver architecture docs/architecture/sourceledger.architecture.json docs/architecture/sourceledger.html --quality showcase --json
node $archifyCli visual-check docs/architecture/sourceledger.html --json
```

Run `visual-check` only after `deliver` succeeds for the same JSON bytes. `deliver` establishes deterministic artifact checks; `visual-check` records automated browser evidence; a human or image-capable review establishes visual polish. These are distinct claims. Regenerate the PNG preview from the delivered local HTML when the diagram changes, then review the resulting files before committing. Consult the generated receipts for the current validation and browser-evidence status; this guide does not replace them.

## Maintenance rule

Update the JSON and regenerate the HTML/preview whenever runtime components or relationships change. Keep external services outside the implemented boundary until this repository validates and connects them. The diagram intentionally does not describe credentials, browser profiles, research workspaces, databases, collected documents, or generated reports as repository artifacts.
