# Collection reliability

Recent coverage and checkpoint changes: [2026-10-14 implementation](CROSS_SITE_RELIABILITY_2026-10-14.md).

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

Research-plan refreshes reuse the collection checkpoint, durable issue ledger, browser recovery, and eligible HTTP validators. New plans default to `full`. Existing explicit settings are preserved. A reviewed plan may opt into `incremental` with a full recheck interval from 1 to 365 days (default 7); its first run, elapsed interval, changed rules, or invalid baseline makes the next run full. This is a due-on-run decision, not an hourly scheduler: no run starts merely because the due time passes. Existing confirmed plans without a refresh policy retain full collection. Browser pages must be visited again; a prior browser session or capture is not silently reused.

Refresh comparison is scoped to the approved URL set and pages actually visited. SQLite retains the full per-URL change record and XLSX presents the human-readable comparison; MCP results expose a bounded 50-entry summary. Added, changed, unchanged, unavailable, and removal candidates are distinct. A missing item is called a removal candidate only when the prior and current page inventories are complete; a blocked or incomplete page remains unavailable. Same-host link discovery does not prove whole-site completeness, and no missing price, currency, identifier, or commercial condition is inferred.

While a job runs, confirmed CAPTCHA/access challenges and authentication requirements produce bounded attention items in MCP progress and a nonblocking UI notification and review list. Generic HTTP 403 and policy denial alone are not browser-handoff alerts. The queue continues with other URLs and retains each blocked page as a coverage gap. Opening a link does not move the user's browser session to the collector or automatically resolve an issue; recovery still requires the explicit evidence and triage flow.

URL policy failures use stable diagnostic codes for invalid URLs, embedded credentials, domains outside the allowlist, DNS resolution failure, private addresses, and special-use addresses. Observable diagnostics omit query values, credentials, cookies, and resolver error detail. Robots 404/410 means no rules were found; authentication and access failures remain unavailable rather than being treated as absent rules. HTTP 403 and 429 are recorded as distinct access and rate-limit blocks.

Browser recipes can click, select, fill, wait, and assert only as configured. They are reviewed for read-only use, and the collector prevents native form submission; this does not guarantee that page JavaScript will not make other requests. Browser evidence preserves live select, checkbox, and radio state in a sanitized HTML snapshot, alongside a screenshot and safe control labels in the trace. Sanitization removes captured form input and textarea values; it does not promise to remove every cookie, token, contact detail, or script-provided value that may appear elsewhere in page HTML.

See [Milestone 04](MILESTONE_04.md) for the implemented scope and boundaries.

The address classifier handles the well-known NAT64 prefix by checking the
embedded IPv4 destination. Public destinations are allowed; private, loopback,
link-local, multicast, and reserved destinations remain denied. This follows
the filtering principle in [RFC 6052](https://www.rfc-editor.org/rfc/rfc6052#section-3.1).


### Choosing a refresh mode

Both modes compare the current observations with the last valid baseline. Incremental mode can reduce transfer and extraction work for eligible HTTP sources with valid retained evidence; it does not identify changed dynamic pages without visiting them. For browser-heavy targets, choose full mode unless a same-inventory measurement shows an incremental benefit. Time spent comparing results alone is not a speed improvement.

Catalog HTTP extraction reuse requires matching bytes, final URL, media type, source configuration and extraction rules. Raw candidates are restored before applying the current scope filters and validation. Extraction errors are explicit failures: they must not overwrite the last valid comparison baseline or advance the successful full-check timestamp. A failed page remains a gap, even if another response preserved some observations.

Browser workers receive a shared 15-second teardown grace period. This avoids treating ordinary Chromium shutdown beyond two seconds as a collection failure. A worker still alive after the grace period remains an error; no overlapping retry or detached browser is accepted as completed.

### Optional UI dismissal

A reviewed `click` recipe may set `if_visible: true`. An optional `appearance_wait_seconds` (0–5) allows a delayed overlay to appear within the collection deadline. A missing visible target is recorded as a skipped step; one visible target is clicked, and multiple matches fail closed. This is used to close a known promotion overlay before opening Gongcar's read-only price sheet. It does not submit quote forms or skip CAPTCHA/authentication checks.


### Current Gongcar price sheets

Current `/monthly` and `/daily` redirects are supported alongside the legacy rental paths. For long-term rental, rentpass and monthly rental, the collector opens the visible full-price sheet and extracts each displayed period and payment-condition cell separately. Declared vehicle counts are checked against visible vehicle groups; hidden, missing or unparsed cells leave a partial result. Inquiry-only cells remain observations without a price. Starting-price cards retain their `from` qualifier and review status, and are not substituted for the detailed sheet.

A unique source `data-id` anchors condition-specific identity where present. Missing or duplicate identifiers fall back to explicitly uncertain local observations. Public quote tables do not establish vehicle stock or a binding offer; source dates and commercial conditions stay attached to the observations.

## Item-level change review

Full collection is the default for new research plans. It still compares each approved page against its last valid saved baseline. Existing plans with an explicit incremental policy are not silently changed. The first capture is labelled **First capture**, not **Added**; **Added** means newly observed relative to that page's prior capture, not a confirmed market launch.

Run details provide status filters and paginated before/after observations. The XLSX **Item Changes** sheet preserves every item, including observed prices/currencies, conditions, changed fields, source URL, and previous/current check times. SQLite is the source of truth. UI and MCP `get_collection_changes(job_id, status, offset, limit)` read its complete history, beyond the 100-item progress preview. `status="changes"` excludes unchanged and first-capture rows. Older runs without item detail are explicitly identified; no historical before/after values are invented.

Price and currency changes are distinguished from changes in other observed fields. Only unambiguous source identifiers and matching variant specifications are paired. Unmatched or ambiguous options remain separate newly observed/unavailable entries. A failed check leaves the current value blank and retains the prior baseline. Missing items become possible-removal review items only when both inventories were verified complete; neither status confirms discontinuation or zero price. Check times describe collection attempts, not a seller's effective price-change date. Controlled test changes and replayed historical source captures are labelled separately from new live market observations.
