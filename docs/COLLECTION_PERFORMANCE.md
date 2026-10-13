# Collection performance

Measure discovery, collection, evidence validation, and AI orchestration separately. A faster run is useful only when the same URL manifest, offer variants, source evidence, and coverage checks are preserved.

## October 2026 experiment

An isolated two-worker experiment called the existing `collect_plan` engine directly with independent output directories, 20-URL batches, link following disabled, and fresh HTTP/browser collection. It did not use a model API or an AI CLI. The engine wrote SQLite, source evidence, and XLSX reports. This experiment did **not** enable the MCP durable checkpoint path; it is not a measurement of the default MCP workflow and is not a shipped parallel scheduler.

On the same seven-page sample, one worker took 22.50 seconds and two workers took 18.49 seconds (17.8% shorter). All 25 price observations, conditions, and evidence modes matched. This is one small comparison, not a universal throughput guarantee. The larger 490-URL run took 1,166.58 seconds. Summed HTTP and browser work was 393.72 and 1,780.20 seconds respectively; because workers overlap, these sums must not be presented as elapsed time.

Catalog and detail coverage are different. Short-rental collection now reads monthly and daily catalog cards and their detail pages. Monthly period prices are separate offers; catalog starting prices are not additional distinct offers. A declared count must match the extracted catalog count. Dynamic scroll sentinels must use the `scroll` expansion trigger, not a click that could land on a newly inserted product card. A missing declared count or unexpanded list is not complete coverage.

## Checkpoint-enabled MCP comparison (2026-10-13)

The same fixed 525-URL manifest was collected through real stdio MCP calls (`create_research_plan`, update/confirm, `start_research_plan`, `wait_for_job`, status/gap/report retrieval). Both modes used fresh workspaces, 20-URL batches, a 600-second per-batch limit, no link following, the same code, and the same storage drive. The worker persisted checkpoints under the actual MCP job IDs.

| Mode | Collection stage | Including MCP setup and plan preparation | Observations |
|---|---:|---:|---:|
| Sequential, one workspace/worker | 2,091.248 s | 2,092.976 s | 2,009 |
| Parallel, two isolated workspaces/workers | 1,191.156 s | 1,194.141 s | 2,009 |

Collection elapsed time fell by **900.092 seconds (43.04%)**. All 525 URLs and semantic observation multisets matched: amounts, raw fields, rental conditions, review status, evidence mode, and visibility. All 39 reported catalog/detail sections were complete in each run. All 54 checkpoints were complete, with matching job IDs, capture counts, and budgets. Referenced content, receipt, and screenshot files passed hash checks. Of the 2,009 observations, 2,006 had rendered visible evidence and three remained static/unconfirmed; all retained review status. Catalog/detail overlaps are observations, not distinct products. Run-generated identifiers differed as expected and were excluded from semantic equality.

This is one sequential-then-parallel live comparison, not a statistical guarantee. It measures the actual MCP service and worker path, **not AI-app reasoning latency**. It uses two independently supported workspace workers; one workspace still executes its queue serially. No production parallel scheduler or default was changed. Checkpoint persistence was audited; this experiment did not inject a crash to test recovery. Research data and the full comparison audit remain outside Git in the local benchmark directory.

## Subscription AI app end-to-end comparison (2026-10-13)

One ChatGPT-subscription-authenticated Codex CLI session using the configured `gpt-6-astra` model at medium reasoning handled each mode from request to final Korean answer. It planned, updated, confirmed, started, waited, and inspected the jobs through SourceLedger stdio MCP itself; no model API key was used. The sequential run used one MCP workspace, and the parallel run used one AI session orchestrating two independent MCP workspaces. The CLI measurement includes model and tool-call latency but excludes desktop UI repaint. Both runs used the same fixed 525-URL manifest (same SHA-256), 27 batches of at most 20 URLs, automatic durable checkpoints, 600 seconds per batch, no link following, and fresh workspaces. Sequential ran first, then parallel.

| Measure | Sequential | Parallel |
|---|---:|---:|
| Request to final answer | 3,311.337 s | 2,086.969 s |
| Request to first MCP call | 9.727 s | 11.074 s |
| First MCP call to first collection dispatch | 21.000 s | 36.331 s |
| First collection dispatch to last observed terminal job | 3,229.347 s | 2,018.503 s |
| Last terminal observation to final answer | 51.263 s | 21.061 s |
| Aggregate input tokens, including cached | 12,839,908 | 7,109,087 |
| Cached input tokens (subset of input) | 12,694,144 | 7,004,160 |
| Uncached input tokens (derived by subtraction) | 145,764 | 104,927 |
| Output tokens | 34,031 | 11,724 |

The request-to-answer interval was **1,224.368 seconds (36.975%) shorter** in this pair. Both sessions created and confirmed 27 plans, started and observed success for 27 jobs, and read 27 report records. The MCP call elapsed-time union was 2,366.768 seconds sequentially and 1,900.525 seconds in parallel; these overlap the journey phases above and must not be added to them. Durable job envelopes also include queue time. Time outside MCP calls includes model processing, network and CLI overhead and is not a pure reasoning measure. Provider token usage is available only for the complete session, not per phase; cached tokens are already included in input tokens.

This is one live pair, not a statistical estimate of all future runs. The semantic audit found 2,009 observations in each run with identical recorded prices and no integrity errors across 54 completed checkpoints. The observation rows were **not fully identical**: 10 rows from three Jetcar URLs differed in non-price fields. At `/1977`, five option fields changed from `순정네비게이션` to `앞좌석 AV 모니터`; at `/2216`, two odometer fields changed from 20 to 500 km; at `/2218`, three rows no longer included `파워전동트렁크` in options. The same differences are present in the saved rendered HTML, and the Korean text is intact. An independent per-URL multiset comparison confirmed that all 2,009 amount/currency observations match. Why the source pages differed between captures has not been established, so equal price totals do not prove equivalent vehicle specifications. Differences in model planning, context growth and live site responses can affect both time and tokens, so the observed saving cannot be attributed solely to two workers. Local event logs, workspaces and timing analysis remain outside Git.

## Optimization order

1. **Bounded concurrency:** retain a fixed manifest and compare one versus two workers with identical checkpoints, cache policy, screenshots, and evidence validation. Keep separate ledgers or serialize writes. Preserve per-host rate limits, retry budgets, cancellation, and resume accounting. Do not increase concurrency after throttling or challenges.
2. **Browser reuse:** keep a browser process per worker while creating fresh isolated contexts for each public page. Compare cold and warm runs, browser crashes, profile isolation, and unchanged evidence. Current per-page browser startup is a candidate bottleneck; its isolated saving has not been measured.
3. **Incremental checkpoints:** store immutable capture blobs once and update small queue/state records. Current checkpoints serialize and rewrite accumulated captures, including binary data. Preserve backward-compatible reading, transaction consistency, interrupted-page budget charging, and resume behavior. A seven-capture offline replay measured 1.76 seconds without checkpoints versus 3.14 with them; this omitted screenshots and is not a production saving estimate.
4. **Avoid redundant work carefully:** use one detail page when it visibly exposes all rental periods; reuse unchanged evidence only with freshness and conditional-fetch proof. Do not skip a backend merely because another is faster: some offers exist only in one response, and static HTML does not prove rendered visibility.
5. **End-to-end metrics:** record monotonic stage durations, selected model and actual provider-reported usage separately. The earlier engine-only and direct-MCP runs have no model inference stage; the subscription AI run above includes model orchestration. Missing per-phase token telemetry must remain unavailable, never estimated as observed usage.

## Acceptance criteria

- Compare a fixed URL manifest and condition set, and record live catalog changes separately.
- Compare semantic observation sets, declared/observed/extracted counts, unresolved prices, and proof modes—not row count alone.
- Validate source files, receipts, screenshots, and hashes; incomplete commercial conditions remain under review.
- Report successful-run latency, retry/failure overhead, and development/audit time separately. For split runs, call the sum a collection-stage total, not a single continuous wall-clock run.
- Do not extrapolate a small sample into a claimed whole-site speedup. Historical runs with different scopes or checkpoint settings are reference points only.

Generated research manifests, prices, databases, and evidence stay in ignored local workspaces.

## Checkpoint storage implementation

The first checkpoint storage optimization is implemented: binary captures are stored once per content hash, with lossless compression for suitable content. See [storage replay measurements](STORAGE_OPTIMIZATION.md). Bounded HTTP scheduling and server batching are implemented; browser process reuse remains future work. Original runs are unchanged.

## Server batch subscription measurement (2026-10-13)

The [server batch workflow](SERVER_BATCH_COLLECTION.md) ran through ChatGPT subscription authentication with explicitly selected `gpt-6.1-sol`, medium. The same ordered 525 URLs ran in 27 batches (20 URLs, last 5), 600 seconds per batch, fresh checkpoints, no link discovery, HTTP concurrency 2 and browser concurrency 1. No model API key was used.

| Measurement | Previous improvement, Astra | Server batches, Sol 6.1 |
|---|---:|---:|
| Request to final response | 2,680.173 s | 1,894.873 s |
| Collection-stage sum | 1,547.485 s | 1,747.689 s |
| Remaining journey time | 1,132.688 s | 147.184 s |
| MCP calls | 322 | 60 |
| Price/currency observations | 1,969 | 2,009 |
| Input tokens, including cache | 11,088,157 | 2,874,695 |
| Cached input, subset above | 10,943,872 | 2,783,360 |
| Output tokens | 33,957 | 7,564 |

End-to-end time fell 29.30% against the immediately preceding incomplete run and 9.20% against the successful two-workspace parallel run (2,086.969 seconds). Model and orchestration both changed; this is not an isolated code or model comparison. Collection itself took longer; remaining journey time includes planning, tool delivery, waiting and review, not just reasoning.

All 525 URLs were visited: Jetcar 1,777 observations and Gongcar 232. Gongcar long-term expansion reached 64 popular cards plus 3 promotions with 15 clicks, restoring 40 missing rows. Rentpass had 21 rows, monthly catalog/detail 130 and daily 14. Per-URL price/currency multisets matched the successful baseline exactly. Four Jetcar URLs differed in non-price fields. This verifies the fixed inventory, not newly published URLs or independently verified quotes.

The audit checked 27 finished checkpoints and 3,144 distinct evidence files across both runs with zero integrity errors. New storage was 1,158,971,624 bytes (1,105.28 MiB): XLSX 1.94 MiB, original evidence 585.05 MiB, SQLite 43.34 MiB, checkpoint blobs 472.22 MiB plus logs. The 23.88% reduction against the successful parallel run also includes live evidence differences.

Collection now accounts for 92.23% of the journey. Next measure per-page Chromium startup and safe process reuse, followed by bounded browser concurrency with isolated contexts and unchanged evidence requirements. Do not shorten completion checks simply to improve timing. Future browser speedup is unmeasured. Local audit and research artifacts remain outside Git.
