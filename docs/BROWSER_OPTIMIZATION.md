# Browser reuse and bounded concurrency

## Goal and invariants

Reduce browser startup and page-wait overhead without weakening catalog completion,
source evidence, approval boundaries, or checkpoint accounting. Keep SQLite writes
on the coordinator. Never share synchronous Playwright objects between threads.

The initial implementation is opt-in through `browser_reuse` and
`browser_concurrency`. Existing callers retain reuse disabled and one browser
capture at a time. The browser limit must not exceed the global collection limit.
The HTTP and browser stages must share site pacing and global limits.

Each dedicated browser worker owns its Playwright driver and Chromium instance.
A reusable process gets a new context for every URL; contexts, routes, and listeners
are closed after capture. Persistent login profiles are excluded from this pool.
Pool lifetime is one collection batch; cross-batch reuse is a separate experiment.

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
