# Collection performance

Measure discovery, collection, evidence validation, and AI orchestration separately. A faster run is useful only when the same URL manifest, offer variants, source evidence, and coverage checks are preserved.

## Measurement scope

For a valid comparison, fix the approved URL manifest, page and time limits, checkpoint settings, evidence requirements, and hardware. Report collection time separately from AI-app orchestration. A smaller or incomplete collection is not a speed improvement.

## MCP comparison

Use the real stdio MCP workflow with fresh workspaces and the same approved manifest. Compare URL coverage, semantic observations, conditions, checkpoints, and evidence hashes. Keep measured results and research artifacts in an ignored local workspace; this public document does not publish site-specific run data.

## Subscription AI comparison

Measure from the initial request to the final response, including model planning and tool calls. Record the exact selected subscription model and provider-reported tokens when available. Do not infer per-stage tokens from a session total. Repeat comparisons with identical site scope and separately report live-site differences.

## Optimization order

1. **Bounded concurrency:** retain a fixed manifest and compare one versus two workers with identical checkpoints, cache policy, screenshots, and evidence validation. Keep separate ledgers or serialize writes. Preserve per-host rate limits, retry budgets, cancellation, and resume accounting. Do not increase concurrency after throttling or challenges.
2. **Browser reuse:** the optional worker pool keeps a browser process per worker while creating fresh isolated contexts for each public page. Compare cold and warm runs, browser crashes, profile isolation, and unchanged evidence. A site-specific speed benefit must be measured under the same conditions.
3. **Incremental checkpoints:** immutable captures are stored by hash while queue and state records are updated separately. Preserve backward-compatible reading, transaction consistency, interrupted-page budget charging, and resume behavior. Isolated checkpoint-write timing is not an end-to-end saving estimate.
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

The first checkpoint storage optimization is implemented: binary captures are stored once per content hash, with lossless compression for suitable content. See [checkpoint storage](STORAGE_OPTIMIZATION.md). Bounded HTTP scheduling, server batching, and optional browser process reuse are implemented. Original runs are unchanged.

## Server batch measurement

A confirmed URL inventory can be processed in bounded server batches through [the batch workflow](SERVER_BATCH_COLLECTION.md). Record request-to-answer, collection-stage time, MCP calls, evidence integrity, and model usage separately. The local research record holds any measured site-specific results; no general speedup is asserted here.
