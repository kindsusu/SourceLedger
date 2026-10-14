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

## Experiment protocol

The B path also introduces completion-order scheduling, so A versus B measures
reuse plus scheduling together; B versus C isolates the browser concurrency limit.

1. Freeze a representative subset from the existing 525-URL Jetcar/Gongcar inventory,
   including longterm load-more, rentpass, monthly/daily catalogs and details, and
   Jetcar new/used/cargo examples. Keep URL order, evidence requirements, 20-URL
   batches, 600-second batch limits, and fresh enabled checkpoints fixed.
2. With the explicitly selected ChatGPT-subscription `gpt-6.1-sol`, compare A:
   reuse off/browser 1; B: reuse on/browser 1; C: reuse on/browser 2. Global HTTP/browser
   request limit remains 2. Do not run competing crawls simultaneously.
3. Validate URL and section coverage, per-URL observations, prices/currencies,
   commercial conditions, and evidence hashes. Investigate live source differences
   instead of forcing output to match historical values.
4. Compare the available, exact subscription models on the same representative
   workload and winning collector setting. Never silently substitute an alias or
   charge a model API when a requested subscription model is unavailable.
5. Run the explicitly requested Sol 6.1 and the fastest valid sample model,
   Claude Haiku 5.5, on the full frozen inventory after the sample correctness
   gates pass. Sample ranking alone does not establish full-site performance.

Measure request-to-final-answer time, collection wall time, cumulative worker
times, browser starts/restarts, actual concurrency, errors/retries, evidence volume,
and tokens when the subscription CLI exposes them. Missing usage metrics remain
unavailable. A faster incomplete run does not qualify as an improvement.

Live reports and artifacts belong in the local benchmark output directory and are
excluded from Git. Record actual results after execution; predicted speedups are
not measured results.

## Representative run results (2026-10-13)

The frozen 20-URL sample is a subset of the prior 525-URL run: four Jetcar new,
four used, three cargo, four Gongcar category pages, three monthly details, and
two daily details. Its manifest SHA256 is
`a33c233cc54dfd1bad1af6848f91165f1fbc9982767d8ca846d1bd87b07cc720`.
Each run used a fresh checkpoint, one approved server batch, a 600-second limit,
global concurrency 2, and the same evidence requirements. Runs were sequential.

| Sol 6.1 setting | Request to final answer | Collection wall | Browser launches | Actual peak browser captures |
|---|---:|---:|---:|---:|
| A — reuse off, browser 1 | 235.93 s | 134.25 s | 20 | 1 |
| B — reuse on, browser 1 | 166.18 s | 98.16 s | 2 | 1 |
| C — reuse on, browser 2 | 139.04 s | 59.30 s | 2 | 2 |

From A to B, request-to-answer time fell 29.6%; from B to C, 16.3%. A to C
fell 41.1%. A versus B also changes completion-order scheduling, so its gain
cannot be attributed solely to process reuse. Browser-launch time summed 5.03,
0.64, and 0.47 seconds respectively; the larger collection-wall reduction also
reflects scheduling and overlapping waits. The 20-URL sample produced 169
observations in every setting, with no missing URLs, price/currency differences,
or semantic-observation differences against the prior sample. Evidence and
checkpoint audits found no errors.

With setting C fixed, the five subscription-model samples measured:

| Requested model | Request to final answer | Collection wall | Price/semantic differences vs prior sample |
|---|---:|---:|---:|
| GPT Sol 6.1 | 139.04 s | 59.30 s | 0 / 0 |
| GPT Luna 6 | 114.71 s | 62.50 s | 0 / 0 |
| Claude Opus 5.5 | 110.18 s | 63.94 s | 0 / 0 |
| Claude Sonnet 5.5 | 256.83 s | 208.22 s | 0 / 0 |
| Claude Haiku 5.5 | 99.51 s | 66.00 s | 0 / 0 |

These are single live runs, not stable model rankings. Sonnet's long collection
wall included longer page navigation and evidence capture (59.89 and 67.22
seconds summed, versus roughly 13–17 seconds for other C runs); the measurement
does not establish a model-caused collector slowdown. The server performs the
approved batch, so model differences chiefly affect setup, tool interaction,
and final review, while site and network variation affects collection. Claude
result metadata and assistant messages confirmed the requested exact Opus,
Sonnet, and Haiku 5.5 IDs. Codex used explicit `-m` IDs for Sol/Luna, but its
event stream did not expose a separate backend model ID. Both CLIs used
subscription authentication; no model API was invoked. The maximum memory
measurement was unavailable; it is not estimated.

Full 525-URL C runs and the comparison with the prior 31m 34.87s Sol 6.1
server-batch baseline are pending. The full manifest remains
`01fe8e7fc585cc0d5292b0cf030befcf000b421c6ee5c16ec9419eee98faa8d7`.
The sample result is not a claim that the full catalogs are complete or faster.
