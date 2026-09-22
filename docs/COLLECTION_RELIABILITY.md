# Collection reliability

SourceLedger keeps observations tied to the content and conditions that produced them. It records source material in SQLite and writes a human-readable XLSX report; the report is not a replacement for the retained evidence.

## Research scope and supported pages

Start with `source-ledger init`. The workspace asks for industry, product, and market before collection. After that, `collect-sites` accepts only explicit, authorized URLs for the supported Jetcar, Gongcar, and Funrent page formats.

```powershell
source-ledger collect-sites `
  --url "https://www.jetcar.kr/sub0201/<vehicle-id>" `
  --max-pages 5 --max-seconds 120
```

The command uses the default `.sourceledger/research.json` workspace created by `init`; use `--workspace PATH` only for a separate topic. Use `--no-incremental` to disable conditional retrieval. The command gathers supplied supported detail or rendered-calculator pages, respects the page and collection time bounds, and does not submit quote forms. It does not discover every listing on a site, infer data from unselected pages, or claim whole-site inventory coverage. No paid MCP service is part of this flow.

## Price profiles, origin, and conditions

`Product.price_profile` is either `unit` or `rental`. The unit profile retains the normal product-price comparison rules. The rental profile preserves quote conditions as evidence-backed fields. It treats `deposit_amount`, `advance_amount`, `deposit_installment_amount`, and `deposit_installment_months` as distinct conditions. A collection must not add, subtract, or substitute those values.

Supported `Source.adapter` values are `jetcar`, `gongcar`, and `funrent`. They are deterministic parsers for the named rendered formats. Their source HTML and browser state remain evidence. An extractor does not execute source scripts; a browser backend may execute the page's normal JavaScript to render the reviewed page state.

- `observed` is a value visibly displayed by the source and backed by evidence.
- `calculator_estimate` is a displayed calculator output. It stays an estimate and does not become an observed price or a comparable unit quote.
- `visible` is required for a rental quote to be comparable. Hidden content or missing, conflicting, or incomplete evidence is retained for review rather than used in comparison.

The adapters mark raw or static HTML prices `unconfirmed` because HTML alone does not establish computed CSS visibility. They retain the price, but it is not comparable until display evidence is captured. Structured files and document records are unaffected. When HTML display proof is deficient, collection may retry the evidence capture; it does not retry indefinitely merely because commercial terms are missing. Playwright captures preserve computed hidden state before marking visible rows. A verified amount means that source fields passed validation; the quote can still be unsuitable for comparison when contract conditions are missing. Conflicting amounts remain in the ledger with review status. The history includes a separate unverified observed amount column, and Rental Quotes retains reviewed amounts alongside their status and review reason. Source Evidence records screenshot and receipt references when they are captured.

## Reported evidence and comparisons

When a run contains rental observations, XLSX adds a **Rental Quotes** sheet. It places observed monthly prices and calculator estimates in distinct columns, and keeps deposits, advance payment, and deposit installments as distinct conditions. It is absent for runs without rental observations.

**Observation History** retains observed and estimated amounts separately with raw fields, field evidence, value origin, source visibility, verification level, derived values, and rental conditions. Price-comparison statistics only include verified, visible observed values; estimates and hidden-source values are not comparison inputs.

## Conditional retrieval and extraction reuse

Ordinary `Source.incremental` is disabled unless a source explicitly sets `"incremental": true`. `collect-sites` enables it for generated public HTTP sources by default; pass `--no-incremental` to that command to disable it. Incremental retrieval is limited to eligible public HTTP web sources. It does not apply to sources with browser recipes or profiles, internal sources, non-public account scopes, files, or browser collection.

For an eligible source, SourceLedger may retain ETag and Last-Modified metadata. The next run still issues a request. Only a matching HTTP `304 Not Modified` response lets it reuse retained content and prior extraction. It then validates the reused candidates again for the new run, including date-based validity. Identical newly retrieved bytes may reuse extraction; changed bytes are extracted again.

Before sending validators, SourceLedger checks that retained evidence is in the workspace evidence directory, readable, and hash-matched. Missing or altered evidence causes a fresh request without validators. A change to source rules, recipe version, account scope, adapter/extractor code, or other cache-keyed source configuration also causes a fresh request. Cached content is therefore never claimed to be newly retrieved without a fresh retrieval or a validated conditional response.

## Access and browser evidence

URL policy failures use stable diagnostic codes for invalid URLs, embedded credentials, domains outside the allowlist, DNS resolution failure, private addresses, and special-use addresses. Observable diagnostics omit query values, credentials, cookies, and resolver error detail. Robots 404/410 means no rules were found; authentication and access failures remain unavailable rather than being treated as absent rules. HTTP 403 and 429 are recorded as distinct access and rate-limit blocks.

Browser recipes can click, select, fill, wait, and assert only as configured. They are reviewed for read-only use, and the collector prevents native form submission; this does not guarantee that page JavaScript will not make other requests. Browser evidence preserves live select, checkbox, and radio state in a sanitized HTML snapshot, alongside a screenshot and safe control labels in the trace. Sanitization removes captured form input and textarea values; it does not promise to remove every cookie, token, contact detail, or script-provided value that may appear elsewhere in page HTML.

See [Milestone 04](MILESTONE_04.md) for the implemented scope and boundaries.

The address classifier handles the well-known NAT64 prefix by checking the
embedded IPv4 destination. Public destinations are allowed; private, loopback,
link-local, multicast, and reserved destinations remain denied. This follows
the filtering principle in [RFC 6052](https://www.rfc-editor.org/rfc/rfc6052#section-3.1).
