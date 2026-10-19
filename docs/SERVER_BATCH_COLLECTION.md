# Approved inventory collection

SourceLedger can collect a reviewed URL inventory with one approval and one durable server job. The connected AI app plans and reviews the work; the local server advances through the batches without asking the model to create and confirm another plan for every batch.

## MCP flow

1. Create a research plan and add the exact selected URLs with `update_research_plan`. Explicit inventories support up to 1,000 candidates; AI-generated previews remain limited to 100.
2. Call `get_research_batch_execution_plan` and explain the selected sites, target count, batch count, per-batch limits and connected app's model to the user.
3. Confirm the reviewed plan revision once with `confirm_research_plan`.
4. Call `start_research_batch_run` with the confirmed revision. Defaults are 20 URLs per batch, 600 seconds per batch and HTTP concurrency 2. Link discovery is disabled for this fixed inventory.
5. Wait for the job and inspect `get_research_batch_progress`. Use `get_research_batch_reports` for paginated batch results and XLSX locations.

The server pins the confirmed plan fingerprint and URL order. Each batch has a separate checkpoint and report; all batches use the plan's SQLite database and evidence directory. The existing single-plan job limits remain unchanged. HTTP concurrency does not imply concurrent browser captures: browser fallback is serial by default. Set `browser_reuse=true` and `browser_concurrency=2` explicitly in both preflight and start to enable the experimental bounded browser pool; the browser limit cannot exceed the global concurrency. See [browser optimization](BROWSER_OPTIMIZATION.md).

Completed batches are recorded in a durable journal. Explicitly resuming an interrupted run skips completed batches and uses the current batch's existing checkpoint and remaining budget. A completed partial batch is not silently fetched again. Stop requests are observed between batches; an active batch may take up to its remaining budget to finish.

## Completeness and evidence

An executed job and a visited URL do not establish that a site's entire catalog was collected. Check section completion, missing pages, observation counts and original evidence separately. Values without source evidence remain missing and commercial conditions still require review.

Dynamic list expansion now verifies that the button stays absent and the rendered card list stays stable for the recipe's progress-wait window. If a button reappears, expansion continues within the original click and time limits. Insufficient time to confirm the end is recorded as partial (`completion_unconfirmed`), and replacing a list is not treated as complete. This is a bounded DOM stability check, not proof that the publisher exposes every product.

## Performance measurement

Measure both collection time and the connected app's request-to-final-response time. The latter includes planning, MCP calls, polling and final review. Compare the same URL order, batch limits, checkpoint conditions, browser concurrency and evidence requirements. A model change is a separate experimental variable; do not attribute the entire difference to code.

The feature uses the connected app's selected model and does not require a model API. Live benchmark outputs and original research evidence are local artifacts and must not be committed.
