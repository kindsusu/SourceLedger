# Cross-site collection audit — 2026-10-14

Status at audit time: investigation and proposed work. Subsequent implementation is documented in [Collection reliability changes](CROSS_SITE_RELIABILITY_2026-10-14.md).

[한국어](COLLECTION_AUDIT_2026-10-14.ko.md)

## Conclusion

Fixing the Jetcar cargo adapter is necessary but insufficient. Collection must independently report execution progress, discovery coverage, extraction coverage, and field verification. A successful request or a retained price does not prove that every product or option was collected. For an arbitrary site with no trustworthy inventory or enumeration boundary, completeness must remain **unknown**.

This review covers collection/discovery, adapters and generic extraction, evidence validation, checkpoint/resume, scheduling, result presentation, XLSX coverage, and the batch/MCP/UI summaries. It is not an exhaustive security audit or a fresh crawl of arbitrary public sites. Existing uncommitted browser-optimization work was preserved.

## Evidence and corrections to previous benchmarks

- The retained Jetcar cargo HTML for `/sub0701/16`, `/17`, and `/18` contains five priced contract periods per page. The adapter rejects these paths and the catalog fallback retains one JSON-LD price per page. An in-memory identity correction, without editing production code or fetching again, produces five candidates per page: 12 previously missed rows.
- The previous 20-URL, 169-row baseline is therefore not a completeness oracle. Its time measurements remain measurements of that incomplete workload. Adding only these 12 rows implies 181 rows for that frozen snapshot; the standalone collector's 187 rows also include six observations of different price/condition types.
- The retained representative Sol runs took 235.93 seconds (no reuse, browser concurrency 1), 166.18 seconds (reuse, 1), and 139.04 seconds (reuse, 2), request through final response. A-to-B also changed scheduling. These are single runs with the same omission, not proof of complete collection or a reliable universal speed ranking.
- The standalone Sol run took 1,312.61 seconds and recorded 187 rows. Its scope included additional interactions and price types, and it still marked four pages incomplete. It is not a controlled measurement of framework overhead alone.

## Findings

| Priority | Finding | Code evidence | Required change |
|---|---|---|---|
| P1 | Coverage contracts exist only for Gongcar; other pages can have no section checks despite missing displayed offers. Generic metadata prices are correctly review-only, but zero incomplete sections is still misleading. | `site_collection.py:_sections`; `catalog_collection.py:collect_plan`; `research_batch.py:_summary` | Always emit explicit coverage, including unknown, and propagate it to follow-up, MCP, UI, and XLSX. Keep process completion separate from scope completeness and price verification. |
| P1 | Discovery silently returns only the first 100 links; JSON-LD traversal silently limits arrays to 1,000 and depth to eight. Anchor traversal alone does not enumerate dynamic filters, pagination, or load-more states. | `catalog_collection.py:_links`, `_nodes` | Preserve protective limits, but record truncation, pending work, and stop reasons. Use approved scope, product-link prioritization, and a paginated discovery frontier. Require evidence of the enumeration boundary. |
| P1 | Two generic JSON-LD implementations disagree. `ProductGroup.hasVariant` yields two rows in catalog collection and zero through configured-source extraction in a synthetic example. Array-valued `priceSpecification` loses both explicit prices. | `catalog_collection.py:_jsonld_candidates`; `extraction.py:_extract_html` | Share one parser and one evidence contract across entry points. Preserve variant IDs and price-specification semantics. Never flatten component prices, ranges, fees, or quantity tiers into invented offer prices. |
| P1 | A browser-captured clothing size L at the same URL/name as stored size M remains only in the unlinked evidence list; the main result has one row. Evidence is retained, but the product result is incomplete. | `result_view.py:build_result_view` | Distinguish a proven new variant from ambiguous supporting evidence using full variant/offer dimensions, not rental term alone. Keep uncertain identities as visible review candidates. Test both UI and XLSX presentation. |
| P1 | Finished catalog checkpoints are not bound to the effective parser/coverage rules and can immediately return obsolete candidates. Retrying selected URLs copies other cached captures. | `catalog_collection.py:binding`, completed-result return and parent capture reuse | Version parser, recipe, schema, and coverage policy separately. Reparse retained evidence into a new result when rules change; preserve original snapshots and timestamps. Distinguish resume, reanalysis, and fresh collection. |
| P1 | Jetcar cargo is missing from both identity parsing and discovered-link filtering. | `adapters/jetcar.py:_actual_id`; `catalog_collection.py:_links` | Share route support between discovery and extraction; add five-period fixtures and missing-row coverage tests. Do not use this site-specific correction as the general completeness solution. |
| P2 | Coverage-less pages disappear from the XLSX section audit and the UI section panel instead of showing unknown coverage. | `report.py:append_section_coverage`; `web/app.js:catalogPages` | Include a page-level unknown row, failure/limit reason, and evidence reference even when no section was recognized. Report unknown section counts explicitly in batch/MCP results. |
| P2 | Robots policy is checked for the initial URL, but redirect destinations are not checked against their own path policy before fetching. | `collectors.py:_http_request`, HTTP/browser entry paths | Check each redirect/navigation destination within the allowed scope; add policy regression tests. Do not bypass access controls or challenges. |
| P2 | Browser diagnostics report availability from executable presence, while launch errors lose actionable stage information. | `doctor.py:_playwright_browser_ready`; `collectors.py:_browser_collect` | Distinguish installed from launch-tested, expose a sanitized `browser_launch_failed` reason, and offer a bounded alternative runtime check. Do not silently replace a requested visible browser with headless mode. |

Generic JSON-LD already produces `needs_review`, and the catalog response already states that it makes no whole-site completeness claim. The defect is not that every generic price is marked verified: missing coverage information is not represented strongly enough, and downstream summaries or benchmark audits can mistake it for no omissions.

## Offline reproductions

The diagnostic script and output are local `.tmp/cross_site_audit.py` and `.tmp/cross_site_audit.json`; they use synthetic HTML and make no network requests.

| Fixture | Observed behavior |
|---|---|
| Five displayed option rows, one JSON-LD offer | One candidate; zero coverage sections |
| 100 navigation links followed by one product link | 100 returned links; product link omitted |
| ProductGroup with two explicit variants | Catalog parser: 2 candidates; configured-source parser: 0 |
| Two explicit price specifications in an array | 0 extracted price fields |
| Array of 1,001 Product records | 1,000 visited records |
| Existing size M plus browser-captured size L | 1 main result; 1 separate unlinked evidence record |

The existing `.tmp/cargo-root-cause.json` records the retained-HTML cargo reproduction. Research evidence and diagnostics are not committed assets.

## Proposed cross-site contract

Each run should identify whether its approved scope is a fixed URL list, a category, or a whole site. Counts must state what they count: URLs, unique products, variants, offer conditions, or price observations.

Each page/section needs: discovered/expected count and its source, observed count, extracted identities, explicit exclusions, unparsed records, unvisited interaction states, limit/failure reasons, and evidence references. Unknown expected counts stay null, never zero. Match identities and conditions, not just equal totals. A malformed row, repeated price, or explicit no-price product must not disappear from the denominator merely because parsing failed.

Use separate states:

- Execution: queued, running, finished, interrupted.
- Coverage: complete within the evidenced scope, partial, unknown, excluded.
- Observation verification: verified, review, unavailable, conflicting.

Complete coverage does not imply every price is known or commercially comparable. A source explicitly saying “request a quote” is a captured condition, not a guessed amount. An approved list completed successfully does not prove that the list includes the whole site.

For generic sites, inspect list/detail structure and explicit option controls, validate a proposed extraction recipe against observed examples, and retain uncertainty when the structure cannot be enumerated. Use connected AI/browser assistance on unknown sections rather than asking an AI to reinterpret every already-understood page. Do not invent missing option combinations.

## Optimization after correctness gates

| Change | Expected mechanism | Measurement / constraint |
|---|---|---|
| Retain browser process reuse and bounded concurrency 2 | Overlap page waits and reduce launches | Re-run only after new coverage gates pass; preserve fresh page contexts and login isolation. |
| Host-scoped, short-lived robots cache | Avoid repeated policy requests | Track hits/misses, expiry and failed fetch policy; apply it to redirect checks too. |
| Store immutable page captures once; update small checkpoint metadata | Avoid serializing and hashing all previous captures on every page | Compare checkpoint wall time, bytes written, and recovery integrity. |
| Use completed-future scheduling in the legacy HTTP path | Avoid waiting behind an earlier slow page | Keep presentation order deterministic and host concurrency/rate limits intact. |
| Replay unchanged stored evidence when only extraction rules change | Recover omitted rows without browser visits | Label original capture time; this is reanalysis, not fresh market data. |
| Reuse validated extraction/expansion recipes by site template | Reduce repeated AI decisions and unproductive browser interactions | Invalidate on layout/rule changes; fall back to unknown rather than silently accepting fewer rows. |
| Generate the final workbook after durable collection and coverage finalization | Reduce repeated workbook creation/open-save passes | Profile current export time first; keep atomic writes and original evidence. |

No new speed percentage is claimed. A faster run with lower coverage is not an improvement. Pooling or concurrency must not weaken evidence, omit screenshots required for verification, or suppress retries that address known gaps.

## Implementation and acceptance sequence

1. Add a shared coverage model, explicit unknown counts, truncation reasons, and batch/MCP/UI/XLSX propagation. Correct benchmark wording and replace the old baseline as a completeness oracle.
2. Fix Jetcar route support, unify generic parsing, and repair variant result presentation. Add independent synthetic fixtures for groceries, clothing, components, and rental periods.
3. Version checkpoint semantics and implement retained-evidence reanalysis, preserving old runs. Test rule changes, interrupted workers, corrupted evidence, duplicate requests, and missing captures.
4. Verify a fixed HTML corpus against independently specified product/variant/condition identities. Assert exact identity coverage and proof hashes through SQLite, MCP, UI, and XLSX, including failure cases.
5. Apply measured scheduling/storage/policy-cache optimizations. Compare concurrency 1 vs 2 on the same approved manifest, fresh checkpoint conditions, evidence requirements, and requested subscription model. Never use a model API for the user's benchmark.
6. Only then run requested live validation. Record request-to-final wall time, discovery, browser/network, extraction, checkpoint, report, review, retry counts, disk bytes, and exposed token counts. Repeat runs when estimating speed, and separate site changes from parser changes. Do not estimate unavailable tokens or claim whole-site completeness without enumeration evidence.

## Test execution

Command: `.venv/Scripts/python.exe -m pytest tests -q`.

Result: **500 passed, 6 failed, 1 skipped in 383.03 seconds**. The initial sandboxed command could not collect tests because Windows MIME-registry reading by openpyxl was denied; this result is from the same command restarted with normal local execution permissions.

The six failures are one browser selection/evidence test, two browser collection/policy tests, two agent integration tests, and one research integration test. Their common failure path is browser startup or downstream verification after browser startup failed. A separate local-fixture diagnostic confirms headless collection succeeds while visible Chromium fails at `collectors.py:792` with `BrowserType.launch: spawn UNKNOWN`, before navigation and extraction. This does not identify the underlying OS/runtime cause and does not prove an extraction regression. Diagnostic details are in local `.tmp/browser_failure_audit.json`.

The six failing tests were rerun with the explicit process-local setting `SOURCELEDGER_HEADLESS=1`: **6 passed in 53.58 seconds**. No application defaults or installed browsers were changed. This confirms the tested flows work in headless mode; visible-mode launch remains unresolved. Full-suite success must not be claimed from a conditional subset rerun. The subset JUnit result is local `.tmp/cross-site-audit-headless.xml`.
