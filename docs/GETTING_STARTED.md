# Starting a research workspace

[English](GETTING_STARTED.md) | [한국어](GETTING_STARTED.ko.md)

SourceLedger 0.2 provides a bounded onboarding and collection workflow. An operator supplies the initial topic, authorized source URLs, exact product identifiers, and extraction rules. Saved rules then support repeated collection. Search and AI rule generation are future work.

## 1. Enter the topic

After installing the project as described in the [README](../README.md), run:

```powershell
source-ledger init
```

The terminal asks for industry, product or product group, and target market. For Korean prompts, use `source-ledger init --lang ko`. To run without a terminal prompt:

```powershell
source-ledger init --industry "Industrial components" --product "Process pump" --market "South Korea"
```

The default workspace is `.sourceledger/research.json`, excluded from Git. All research commands accept `--workspace PATH` for a separate topic. Initialization refuses to overwrite existing workspaces. The topic is a discovery lead, not an inferred SKU; missing identifiers remain empty. There is no analysis-purpose field.

## 2. Register and discover sources

Use an actual authorized URL in place of the example:

```powershell
source-ledger source-add --url "https://example.com/catalog"
source-ledger research-status
source-ledger source-discover --source-id <SOURCE_ID> --limit 25
```

`source-add` performs no network access. Read the source ID from its JSON result or `research-status`. `source-discover` fetches the selected seed and stores same-host HTML links or sitemap URLs with discovery provenance. It tries HTTP and then the optional browser if needed; install Playwright as described in the README. There is no recursive domain crawl. A single call returns at most 1,000 candidates; a workspace holds at most 1,000 sources.

Only candidates are stored. Navigation, irrelevant products, and duplicate pages may appear. Scope selection and extraction validation still matter. For an authorized internal web source, explicitly use `source-add --scope internal --url URL`. Internal files are configured later in the collection draft using `kind: file` and an explicit `file_root`.

No search provider is configured by default. `research-status` reports `search_provider_unconfigured`; entering a topic does not silently call a paid search or model service.

## 3. Specify identity and draft the rules

Use source-confirmed identifiers in place of the synthetic example:

```powershell
source-ledger product-set --identifier model=TEST-A
source-ledger draft --output .sourceledger/collection.draft.json
```

Repeat `--identifier KEY=VALUE` for additional exact identifiers. `product-set` replaces the identifier map. Optional repeated `--spec KEY=VALUE` replaces required specifications when supplied; otherwise previous specs are retained.

The draft includes saved candidates and empty extraction rules. Review it before collection:

- Keep relevant, authorized source URLs and remove catalogue/navigation seeds that do not contain the selected product.
- Specify the CSS row and field selectors, structured-data extraction, or file column mappings. See [verification.json](../examples/verification.json) for a synthetic HTML/CSV example.
- Map product identity, raw price, currency, and commercial conditions from the actual source. Preserve missing facts as missing.
- For comparable results, provide source evidence for unit, pack quantity, tax, price type, and `price_basis` (`pack` or `each`). Missing conditions exclude comparison; they do not become defaults.
- Browser `recipe` actions must be explicit. A dedicated authenticated `profile_dir` can be configured in the draft. Login renewal and unseen page layouts still need intervention.

The existing collector automatically parses supported JSON-LD, but discovery does not design or validate new selectors. The research workspace currently holds one product lead; edit the collection config to support multiple exact products.

## 4. Verify and activate

Prepare an optional known-price sample file using [verification.samples.json](../examples/verification.samples.json) as the schema example. Sample values should come from a separate human check, never from guessed prices or search snippets.

```powershell
source-ledger verify --config .sourceledger/collection.draft.json --samples .sourceledger/samples.json --receipt .sourceledger/receipt-v1.json
source-ledger activate --config .sourceledger/collection.draft.json --receipt .sourceledger/receipt-v1.json --output .sourceledger/active-v1.json
source-ledger run --config .sourceledger/active-v1.json
```

Omit `--samples` only when source-consistency validation is sufficient. That mode does not establish agreement with independent known prices. Sample matching checks exact source/product, decimal price, and currency; tax, shipping, and variant labels still require correct source mappings and manual sample review.

`verify` executes collection and requires every configured product to be assigned to a source, every intended task to finish verified, and every source/product pair to have a current comparable observation. It re-extracts the preserved evidence and rechecks values. A failed verification still preserves collected data and reports the reasons. Exit codes are `0` for eligible, `1` for ineligible, and `2` for input/runtime errors.

`activate` rechecks the configuration, evidence, observations, and sample file. Receipts expire after the shorter of 24 hours or the configured source freshness window. Price validity dates are checked again at activation. Use a fresh receipt filename after any rule change or failed attempt. Neither receipts nor active configs are overwritten. This publication uses hard links and requires a filesystem that supports them, such as NTFS or typical Linux filesystems.

Activation creates a standalone config with absolute file paths. It does not register a scheduler or promote research candidates in place. `research-status` describes candidate preparation only; use `status --config PATH --run-id ID` for actual collection results. Manually maintained configs can still run directly without activation.

## 5. Accumulate observations

Keep the same `output_dir` for a research series. Each new `run` appends a run and its observations to SQLite; `run --resume RUN_ID` continues an unfinished run. Evidence files retain their SHA-256 hashes, and XLSX files are generated for each run. Unknown values remain blank. CSV is supported as input, while output remains SQLite plus XLSX.

See [implementation status](STATUS.md) for tested behavior and [the roadmap](ROADMAP.md) for autonomous search, rule generation, scheduling, and client integrations still to be built.
