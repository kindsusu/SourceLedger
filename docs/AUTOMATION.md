# Bounded automation (v0.3)

[한국어](AUTOMATION.ko.md)

SourceLedger v0.3 has bounded `search`, `propose`, and `agent` commands. They prepare and verify collection rules for public or explicitly authorized internal web sources; search results are always public candidates. They do not treat search snippets or model output as prices. No paid API key, hosted service, model download, scheduler, or external service is installed by SourceLedger.

Live SearXNG and Ollama servers have **not** been tested in this project. The protocol paths are covered by mocked tests only. Verify a server you operate before relying on it.

## 1. Prepare the workspace

Create a workspace, set exact identifiers, then read the source IDs from `research-status` or the JSON returned by `source-add`:

```powershell
source-ledger init --industry "Industrial components" --product "Process pump" --market "South Korea"
source-ledger product-set --identifier model=PX-100
source-ledger source-add --url "https://vendor.example/catalog/pump"
source-ledger research-status
```

`source-add` saves an explicit authorized URL without fetching it. Its `id` is the `SOURCE_ID` used by `source-discover` and `propose`.

## 2. Optional SearXNG discovery

`search` has no configured provider by default and performs zero network calls without `--provider-config`. It makes one request only to an SearXNG instance you configure, then saves qualifying HTTP(S) results as public source candidates. It never fetches result pages and never records search snippets as prices.

Copy and edit [search.searxng.json](../examples/search.searxng.json). The example is a loopback server on port 8080; it does not install or start SearXNG. `allow_private_endpoint: true` is required for that loopback endpoint. Use HTTPS for a non-private endpoint and optionally restrict result hosts with `allowed_result_domains`.

```powershell
source-ledger search --provider-config .sourceledger/search.searxng.json --limit 10
source-ledger research-status
```

SearXNG must have its JSON search API enabled. Its API accepts `q` and `format=json`; a `403` is reported as unavailable rather than retried as an alternative service. See the [SearXNG Search API documentation](https://docs.searxng.org/dev/search_api.html).

## 3. Optional local Ollama selector proposals

`propose` can inspect one saved source and write a review-only draft. Without `--model-config`, or with `--max-model-calls 0`, it uses deterministic HTML/structured-data rules only. A model can propose CSS selectors only: it cannot return price values, JavaScript, clicks, browser recipes, commands, or activation decisions.

Copy and edit [model.ollama.json](../examples/model.ollama.json), replacing `YOUR_INSTALLED_LOCAL_MODEL` with a model already installed on your own Ollama server. The intended model configuration is explicit and local-only:

```json
{
  "provider": "ollama",
  "endpoint": "http://127.0.0.1:11434/api/chat",
  "model": "YOUR_INSTALLED_LOCAL_MODEL",
  "local_only": true,
  "timeout_seconds": 30,
  "max_input_chars": 30000,
  "num_predict": 1200
}
```

Set `OLLAMA_NO_CLOUD=1` on the machine running the Ollama server and restart that server manually. A loopback URL alone does not prove that a provider is local. SourceLedger accepts no arbitrary model proxy.

```powershell
source-ledger propose --source-id SOURCE_ID --output-dir .sourceledger/proposals/SOURCE_ID --max-model-calls 0
source-ledger propose --source-id SOURCE_ID --output-dir .sourceledger/proposals/SOURCE_ID-model --model-config .sourceledger/model.ollama.json --max-model-calls 1
```

Use a new output directory for each proposal. The result is a preview/draft and is not verified or activated. The request protocol is the [Ollama chat API](https://docs.ollama.com/api/chat); use the [Ollama FAQ](https://docs.ollama.com/faq/) for server operation details.

## 4. Run a bounded batch

The agent checkpoints its run state. By default it permits zero model calls. `--max-steps` pauses after a bounded number of source steps; `--resume` uses the saved inputs and saved budgets rather than accepting changed ones. `--max-seconds` is a cooperative total-work deadline, not a hard process kill.

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-001 --max-sources 3 --max-seconds 120
source-ledger agent --run-dir .sourceledger/runs/run-001 --resume
```

To let the same batch search first, add `--search-config .sourceledger/search.searxng.json`. To opt into one local model call per available budget, add both `--model-config .sourceledger/model.ollama.json --max-model-calls 1`. You can bypass search with one or more saved source IDs:

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-002 --source-id SOURCE_ID --max-sources 1 --max-seconds 120
```

The agent writes per-source previews/drafts, then performs one fresh combined collection and verification. Final aggregation writes to the shared SQLite ledger and creates one XLSX report under the workspace parent `outputs` directory, including source tasks that failed to yield a price. With the default workspace, this is `.sourceledger/outputs`. Read `collection.report_path`, `collection.config_path`, and `collection.receipt_path` from the JSON result. Reuse the verified config with `run --config PATH` for later collections; this avoids repeating source discovery and model work.

Exit codes for these new commands are `0` for successful search/proposal/completed or deliberately paused agent runs, `1` for review/unconfigured/blocked/budget outcomes, and `2` for input/runtime errors. A budget-exhausted run needs a new run directory and explicitly chosen new budget. A paused run resumes without resetting its budget.

## 5. Activation requires known samples

`--activate` is deliberately stricter. Provide a known-samples file and cover every selected source. The current schema is [verification.samples.json](../examples/verification.samples.json): each sample has `source_id`, `product_id`, decimal-string `price`, and `currency`; the file wrapper is `source-ledger/known-samples/v1`.

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-003 --source-id SOURCE_ID --samples .sourceledger/samples.json --activate --max-sources 1 --max-seconds 120
```

Known samples must come from an independent, current human check. Failed, incomplete, stale, or mismatched evidence remains review material; SourceLedger does not invent the missing value or activate the source.
