# Browser reuse and bounded concurrency

## Goal and invariants

Reduce browser startup and page-wait overhead without weakening catalog completion,
source evidence, approval boundaries, or checkpoint accounting. Keep SQLite writes
on the coordinator. Never share synchronous Playwright objects between threads.

New research plan jobs default to `browser_reuse=true` with
`browser_concurrency=1`. Jobs queued before this default keep their stored
settings on resume. The browser limit must not exceed the global collection
limit. The HTTP and browser stages share site pacing and global limits.

Page workers (`concurrency`) fetch HTTP and extract; browser captures run on
`browser_concurrency` dedicated browser threads. Each browser thread owns its
Playwright driver and one Chromium instance, so `browser_concurrency=1` starts
one Chromium. The sequential path also reuses one Chromium on its own thread.
A reused process gets a new context for every URL; contexts, routes, and
listeners are closed after capture. Persistent login profiles are excluded.
Pool lifetime is one collection batch; robots decisions and pooled HTTP
connections can be shared across batches with `collection_job_scope()`.

## Backend order and waits

- For an adapter whose sections are complete only from a rendered capture, catalog
  collection tries the browser first and fetches HTTP only when the browser
  fails, times out, is unavailable, or extracts nothing. Other adapter pages
  keep HTTP first and stop after it only with complete sections and sufficient
  evidence (static HTML is not sufficient). Jetcar pages are the exception
  described below. `collect_sites` keeps both backends because its products
  come from every response.
- List expansion and enumeration keep a final-capture reserve (up to 15 s). If
  the final capture cannot settle, the rendered DOM is retained, its sections
  are partial (`observation_unsettled`), and its rows need review.
- A list end, a missing or disabled next page control, and a missing
  "more" button need a positive stable window with no pending tracked
  fetch/XHR or loader. A list whose every load was network-driven may confirm
  its end after 2.5 s of such quiet instead of the full progress window.
  Pages without the network tracker keep the previous fixed waits.
- The optional promotion pop-up wait on landing pages is unchanged:
  its absence cannot be confirmed positively, and a late pop-up could block
  the price-sheet click.

## Jetcar: HTTP acceptance with sampled browser verification

Jetcar detail pages expose the same rental table in static HTML as in the
rendered page, and the browser stage (navigation, settlement and evidence
capture) dominates collection time for large Jetcar inventories. Before
enabling this mode, compare the retained HTTP extraction
(`raw_http_candidates` in each checkpoint page record) with the kept browser
rows for a full inventory; differences should be limited to listings that no
longer exist. Sampling then keeps checking this on every job.

Catalog collection (`collect_plan`, research plan and research batch jobs)
therefore handles Jetcar pages as follows:

1. HTTP runs first. When it is fetched, extracts rows, and every Jetcar section
   is complete (the adapter's existing section coverage), the page accepts the
   HTTP result and skips the browser. The page trace records
   `{"event": "browser_skipped", "reason": "http_complete_jetcar"}`.
2. A verification sample still runs the browser: `browser_sample_ratio` of the
   job's Jetcar URLs (default 0.1), at least 5, or all of them when there are
   fewer. Selection is per URL by a stable hash of the job identity and URL, so
   it is reproducible for one job, rotates between jobs, and is spread across
   batches. A sampled page keeps the rendered capture as evidence exactly as
   before, and compares the HTTP and browser row multisets on `price`,
   `currency`, `price_basis`, `term_months`, `deposit_amount`,
   `annual_mileage_km`, `name`, `odometer_km` and `availability`. The trace
   records `browser_sample_verification` with `result` `match`, `mismatch` or
   `unverified` and the row counts.
3. Any mismatch switches every remaining Jetcar page of the job, including
   later batches, to the browser (`browser_required`,
   `jetcar_sample_mismatch_fallback`). A resumed job keeps a fallback recorded
   in its journal, checkpoint or a replayed batch result; otherwise it samples
   its remaining pages again and never assumes an unrecorded pass.
4. HTTP failure, timeout, partial or unknown sections, or no rows use the
   browser as before. A sampled browser failure keeps the HTTP result and
   counts as `unverified`, neither a pass nor a mismatch.

Evidence trade-off: accepted rows are static HTML. They keep
`evidence_mode=static_html` and `source_visibility=unconfirmed`, so they stay
review-only and are not comparable as rendered, visible prices. Previously the
same pages produced rendered evidence. The sample bounds, but does not
eliminate, the risk that an unsampled page differs from its rendered view; and
with `concurrency > 1`, pages already in flight when a mismatch is found keep
their accepted HTTP result.

`browser_sample_ratio` must be greater than 0 and at most 1; `1.0` restores the
previous always-browser behavior. It is a `collect_plan` argument, a research
batch job argument (`start_research_batch_run`), and the `collect-plan`
`--browser-sample-ratio` flag. Run `execution` metadata reports
`jetcar_http_accepted`, `jetcar_browser_sampled`, `jetcar_sample_mismatches`,
`jetcar_sample_unverified` and `jetcar_browser_fallback`; batch progress and
the collection summary aggregate them as `jetcar_sampling`. Gongcar stays
browser-first, other adapters are unchanged, and `collect_sites` still uses
both backends.

## External designs reviewed

- [Crawlee BrowserPool](https://github.com/apify/crawlee-python/blob/master/src/crawlee/browsers/_browser_pool.py): reuse browsers with capacity, retire old instances, and clean up inactive browsers.
- [scrapy-playwright handler](https://github.com/scrapy-plugins/scrapy-playwright/blob/main/scrapy_playwright/handler.py): bounded page/context permits, launch locks, cleanup on exceptions, and browser-disconnection handling.
- [Crawl4AI dispatcher](https://github.com/unclecode/crawl4ai/blob/main/crawl4ai/async_dispatcher.py): memory-aware dispatch and per-domain retry delays. Adaptive memory control is a later candidate, not an implemented guarantee.
- [Scrapy AutoThrottle](https://github.com/scrapy/scrapy/blob/master/scrapy/extensions/throttle.py): latency-based pacing that does not accelerate on fast error responses. A later tuning candidate.
- [ego-lite runtime](https://github.com/citrolabs/ego-lite/blob/main/package/ego-browser/src/browser-runtime.ts) and [page model](https://github.com/citrolabs/ego-lite/blob/main/package/ego-browser/src/page-model.ts): task/page ownership and explicit user handoff. Its helpers depend on the ego browser runtime. At review time its README advertises macOS support and a forthcoming Windows closed beta; it is not a required dependency or part of this performance comparison.

These are design references, not a wholesale framework migration. No new paid
service or model API is required.

## Verification protocol

Compare browser reuse off and on, then compare bounded browser concurrency, on the same approved and frozen URL manifest. Hold batch size, checkpoint and evidence requirements fixed. Record request-to-answer and collection time, browser launches, errors, coverage, prices, conditions, and evidence hashes. A change to scheduling must be reported separately from process reuse. Subscription model selection must be explicit; unavailable models are not silently substituted. Store actual research runs outside Git.

## Result interpretation

Browser reuse and bounded concurrency are implemented controls. Their speed effect depends on the target pages, host pacing, and runtime environment. A representative sample cannot establish full-catalog coverage or a universal model ranking. Compare like-for-like runs and retain per-site measurements in the ignored local research workspace.
