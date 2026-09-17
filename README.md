# SourceLedger

[English](README.md) | [한국어](README.ko.md)

![SourceLedger — Collect information. Preserve its source.](SourceLedger.png)

SourceLedger is a local, evidence-first tool for collecting prices for preconfigured products and sources, preserving the source material, and creating XLSX reports. It never estimates prices or substitutes missing values with zero.

Each collection records the source URL, UTC timestamp, raw fields, CSS/JSON/table location, evidence-file path, and SHA-256 hash. A row can be `verified` only when the product, price, and currency are supported by source evidence. A verified row is excluded from comparisons when its conditions are insufficient (`comparable=false`). For example, SourceLedger does not calculate a normalized unit price without source evidence of whether a price is for a pack or an individual item.

## Quick start

Install with Python 3.11 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test,mcp]"
```

Browser collection requires the optional dependency and Chromium.

```powershell
pip install -e ".[browser]"
python -m playwright install chromium
```

The demo reads synthetic HTML and CSV fixtures only. It does not contain market prices.

```powershell
source-ledger run --config examples/demo.json
source-ledger status --config examples/demo.json --run-id <RUN_ID>
source-ledger observations --config examples/demo.json --run-id <RUN_ID>
source-ledger export --config examples/demo.json --run-id <RUN_ID>
source-ledger doctor
```

`su-crawler` remains a compatible command alias. Use `source-ledger` for new scripts and documentation.

Run a bounded batch with `run --max-tasks N`, then continue it with `run --resume <RUN_ID>`. Outputs are stored below the configured `output_dir`: a SQLite run history, source evidence, and an XLSX report.

## Configuration

Configuration is JSON. Each product has `identifiers` to match against source material; each source has a fixed `location` and `product_ids`. A web source must list its URL host in `allowed_domains`. Internal material is limited to local file sources under `file_root`, or to authorized internal web sources. A file source may read only from its base directory or explicitly configured `file_root`.

```json
{
  "name": "Component price collection",
  "output_dir": "outputs",
  "products": [{"id":"part-a", "name":"Part A", "identifiers":{"model":"A-100"}}],
  "sources": [{
    "id":"vendor-a", "name":"Public catalogue", "kind":"web",
    "location":"https://example.com/catalog/a-100", "allowed_domains":["example.com"],
    "product_ids":["part-a"], "backends":["http", "playwright"],
    "selectors":{"model":".model", "price":".price", "currency":".currency"}
  }]
}
```

The XLSX report contains run summary, collection status, price comparison, observation history, source evidence, and review-required sheets. Comparisons use only verified, current, comparable observations. For each comparison condition, it uses one latest observation per source to calculate minimum, maximum, median, and count. `price_basis` (such as `pack` or `each`) and pack quantity are separate comparison conditions; without evidence they are not compared or normalized. A report covers one collection run, not a monthly historical-price report.

### Extraction rules and browser recipes

For HTML, `selectors` and optional `row_selector` retain a CSS location as evidence. CSV uses the `columns` header mapping; XLSX uses `sheet` and `columns`; JSON supports an array or `items`/`products`; PDF uses named capture groups in `pdf_pattern`. OCR for scanned PDFs is not implemented: documents without a text layer remain review items.

Set `profile_dir` to reuse a dedicated, already authenticated browser profile. It does not automate a login flow or enter credentials. A `recipe` contains only explicit page actions: `click`, `select`, `fill`, `wait_for`, and `assert_text`.

```json
"recipe": [
  {"action":"click", "selector":"button[data-tab='prices']"},
  {"action":"select", "selector":"select#pack", "value":"100"},
  {"action":"wait_for", "selector":".price", "state":"visible"},
  {"action":"assert_text", "selector":".currency", "text":"KRW"}
]
```

### Discovering URL candidates

`discover` reads one configured source once and returns up to the requested number of URL candidates from HTML links or an XML sitemap. Candidates are limited to HTTP(S) URLs in the same `allowed_domains`; fragments, duplicates, and URLs containing credentials are discarded. Candidates are neither price evidence nor automatic source configuration.

```powershell
source-ledger discover --config examples/demo.json --source-id catalog --limit 100
```

Review the product scope and selector/column mappings, then add sources to the configuration yourself. Arbitrary web search, automatic learning, and automatic expansion of collection scope are not provided.

## MCP

The MCP server does not create the collection worker as its child process. On Windows, a client Job Object can terminate child processes when it exits. Start an independent worker in a normal terminal before starting MCP.

```powershell
# Terminal 1: independent worker
python -m su_crawler.worker --config examples/demo.json

# Terminal 2: stdio MCP server
source-ledger serve-mcp --config examples/demo.json
```

MCP rejects a collection request when it cannot see a worker heartbeat. Its tools register tasks, retrieve status and observations, and regenerate XLSX reports within the fixed configuration scope. `streamable-http` supports only local `127.0.0.1`.

## Current limits

- The first-run setup wizard and autonomous agent loop are not implemented. Configure products and sources in JSON before collection.
- Discovery is limited to configured HTML/sitemap sources. Arbitrary web search, automatic learning, and automated login are not provided.
- Claude and ChatGPT UI connections have not been verified. Remote ChatGPT use requires an authenticated deployment and an artifact download path.
- Crawl4AI is optional and has not been installed or validated against live sites in this environment.
- Services that incur cost or external transfer, including UWS, Jina, and Exa, are not connected.
- Agent-Reach informed the doctor/routing design but has no direct runtime integration. ego-lite depends on a macOS app path and is not directly integrated on Windows.
- Windows service installation and scheduling for continuous operation are not implemented.

## Before a real deployment

- SKU/model lists and required identifiers for each product
- Three to five target URLs and their authorized collection scope
- Human-verified price samples, including currency, tax, shipping, and member-price conditions
- De-identified internal CSV/PDF samples, or an authorized internal access path and account scope
- Business definitions for `price_basis`, pack quantity, unit-price calculation, and price type
- The intended connection target (Claude Desktop or ChatGPT) and its execution environment

Scanned-document OCR, collecting outside configured sources, ERP integration, and the first-run autonomous workflow require separate design and validation once the relevant sources and access constraints are available.

See [implementation status](docs/STATUS.md) for implemented scope and validation.
