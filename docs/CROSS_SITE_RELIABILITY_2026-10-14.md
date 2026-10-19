# Collection reliability changes

Follow-up: reviewed dynamic collection and issue management are implemented in the [collection-limit review](LIMITATION_IMPLEMENTATION_REVIEW.md). The validation below describes the earlier change set.

[한국어](CROSS_SITE_RELIABILITY_2026-10-14.ko.md)

This implements the correctness and bounded-runtime changes identified in the [2026-10-14 audit](COLLECTION_AUDIT_2026-10-14.md). It does not establish complete coverage of arbitrary websites.

## Result semantics

- A successful page request is not proof that all products or options were found. Pages without a validated enumeration have `unknown` coverage. Truncated extraction or discovery, failed pages, and unmatched stored offers have `partial` coverage.
- Section audits now reconcile extracted offers against the final SQLite ledger using locator, raw fields (including specifications), and the SHA-256 of the inspected source. Deliberately excluded offers are accounted for separately.
- Unknown and partial coverage remains visible in the MCP job summary, UI, and XLSX section audit, including older runs without section metadata. A complete recognized section does not mean the entire site is complete.
- Generic JSON-LD uses one shared parser. Product variants, explicit unit tiers, and specifications remain distinct. Aggregate ranges and delivery/payment fees are not invented as exact product prices. Embedded metadata remains subject to review.
- JSON-LD products, offers, price specifications, and output candidates are bounded per page. Every exceeded bound is reported. Link discovery reports its 1,000-link bound; it is not a universal pagination adapter.
- Jetcar cargo routes share discovery and extraction support. Rental table coverage checks individual visible terms, prices, deposits, and mileage rather than accepting one product identifier as proof of all terms.
- Browser-submitted evidence with a distinct size, color, component specification, package, or quantity tier can appear as a review candidate instead of silently disappearing behind another variant.

## Resume and runtime behavior

Checkpoint rules include extraction, validation, coverage, and report code. Resuming after a rules change reanalyzes retained captures into a new run without fetching completed pages again. Original captures, collection timestamps, previous checkpoints, and previous reports remain available. This is historical reanalysis, not a fresh price check. Incomplete jobs may still need to fetch pending pages.

Checkpoint progress references immutable page records instead of repeatedly serializing all prior page bodies. The HTTP scheduler consumes completed futures as they finish and preserves approved manifest order in final output. Robots decisions use a bounded cache scoped to a collection job. Existing browser reuse and concurrency controls remain explicit settings.

HTTP and Chromium redirect targets are checked against URL and robots policy before navigation. Chromium follows checked redirects in the same tab, preserving the real destination URL, relative-link resolution, `sessionStorage`, and `window.name`. A failed navigation transition is not reported as a successful capture.

An installed browser executable is not reported as a verified connection. To explicitly test launch in the selected display mode:

```console
source-ledger doctor --verify-browser-launch --browser-display-mode headless
source-ledger doctor --verify-browser-launch --browser-display-mode visible
```

Launch failures have a sanitized `browser_launch_failed` stage. The program does not silently switch a requested visible browser into headless mode.

## Validation and limits

Regression fixtures cover product variants, explicit price tiers, source-to-ledger mismatches, traversal bounds, checkpoint reanalysis, and UI/XLSX coverage. Saved-capture replay verifies parser behavior but does not prove the current site inventory or live speed. Generic interactive option enumeration and full-site boundary checks still require a reviewed recipe. Browser launch capability must be checked in the requested display mode.
