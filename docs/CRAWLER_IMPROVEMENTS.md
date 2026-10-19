# Upstream-inspired crawler improvements

Implemented 2026-10-13 following the [Exa MCP / Scrapy review](UPSTREAM_ARCHITECTURE_REVIEW.md). These are targeted changes to SourceLedger, not a replacement with either project.

## Available behavior

- New research-plan jobs in the UI and MCP default to `concurrency=2`; choose 1–4 before execution. The preview and job use the same setting. Direct `collect_plan` callers and older prepared jobs default to 1 for compatibility.
- Parallel mode overlaps HTTP fetches in bounded batches. All network fetches share a global limit and a per-host limit of 2, with 0.25 seconds between starts on the same host. Browser fallback stays on the coordinator, one browser capture at a time. This preserves a single checkpoint/ledger writer and is not concurrent browser automation.
- HTTP 429, 5xx, timeout and transport failures can receive one additional HTTP attempt within the run budget. Authentication and policy failures are not automatically retried. Host cooldown is shared across workers; valid `Retry-After` values are not shortened to force a retry. Long server-requested waits can exhaust the run budget. Sequential mode retains its existing path without this new retry scheduler.
- The SQLite page queue records URL identity (including query), state, attempts, lease timestamps and expiry. Checkpoints remain the authority for committed captures. Restart reconciles queue rows against them. A leased page without a committed capture is marked interrupted and requires explicit retry; queued work can resume without resetting spent page/time budgets. Lease expiry alone does not automatically refetch a page.
- `get_research_progress(job_id)` returns compact page counts, checkpoint availability, elapsed time and report readiness, without HTML, screenshots or full observations. `wait_for_job` remains bounded; use `get_job_observations` with pagination for details. Completed pages are not verified price observations.
- `prepare_external_discovery` converts Exa results already obtained by a connected app into unverified candidates. It performs no network call and does not activate or pay for Exa. [External discovery](EXTERNAL_DISCOVERY.md) explains provenance and truncation handling.
- [Checkpoint storage optimization](STORAGE_OPTIMIZATION.md) preserves exact bytes while reducing repeated writes.

Codex Sol and Claude Opus remain recommendations for their respective host apps. The program does not launch both models together or change a user's model selection. Parallel collection concerns page fetching, not additional model API calls.

## Validation and benefit boundaries

Offline checks cover real overlapping HTTP workers, combined HTTP/browser concurrency limits, host cooldown isolation, transient retries, interruption/recovery accounting, plan-setting propagation, compact live progress, and Exa suggestions remaining unverified.

Offline tests compare the same saved responses under sequential and parallel settings, including price, raw conditions, and field evidence. This checks correctness but cannot establish live-site speed. Storage and model effects require separate measurements; site-specific data stays in ignored local research workspaces. Exa recall, cost, and blocked-page success have not been measured.

The subsequent [server batch feature](SERVER_BATCH_COLLECTION.md) removes per-batch AI plan/approval cycles while retaining bounded checkpoints and reports. Dynamic list completion now waits for stable button absence and card content, rather than a 0.4-second observation. New list-expansion browser tests passed (14), and related batch/MCP/runtime tests passed (51). The expanded whole suite reported 489 passed, 1 skipped and one intermittent legacy UI worker-start failure; the failed case later passed independently, but its intermittent cause remains unresolved. This is not an all-green full-suite result.
