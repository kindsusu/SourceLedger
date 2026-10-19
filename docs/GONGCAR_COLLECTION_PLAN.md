# Gongcar full catalog collection plan

Status: implemented for the supported public layouts. Live inventory must be rechecked for each collection.

## Implemented behavior

The public `/rentpass` and `/longterm` lists use bounded expansion and section-scoped card extraction. Coverage records the observed sections, expansion stop reasons, and unresolved cards. Offer identity is kept distinct from a source-issued identifier, and missing rental conditions remain under review. SQLite stores the coverage record and XLSX presents it. Site inventory and prices change, so a fixed offer count is not an acceptance criterion.

## Scope and verification

Only publicly displayed offers in the two supported paths are in scope. Monthly/daily rental, private inventory, and submitted consultation quotes are separate workflows. Verify each section against the final rendered page and record whether the scoped More control disappears or an explicit limit stops expansion. Inspect promotional overflow separately. A card is not necessarily a unique vehicle or verified contract price.

## Implementation boundaries

The plan below documents the design requirements used for implementation. In current operation, a page request alone never proves complete catalog coverage. A new layout or unparsed card is an explicit partial or unknown section; it must not be silently treated as complete.

## Implementation sequence

### 1. Define completeness independently from price verification

Add a backward-compatible section result containing section key, discovery status, observed card count, extracted offer count, numeric current-price count, unparsed count, expansion status, termination reason, click count and evidence reference. Use explicit unknown/partial/complete states. A missing heading is not proof of an empty section; require an explicit empty state or return unknown/layout_changed.

Do not infer expected inventory from past runs. List completion only means the observed public section ended. Price conditions may remain review-only after discovery completes. Capture unsupported/malformed cards as gaps without synthesizing numbers. Separate page count, offer count and price-value count.

Owners/files: root integration in `models.py`, `catalog_collection.py`, `site_collection.py`, `assistant_workspace.py` and storage/checkpoint serialization.

### 2. Expand public lists through bounded browser interaction

Implement a reusable, validated expansion primitive in `collectors.py`, configured through a Gongcar page-specific recipe in `site_collection.py`. Require one scoped list container and one scoped More control. Wait for actual cards or a confirmed empty state before testing completion. Select the All category only when necessary and record selected state.

After each click, wait for new card identities/content or a confirmed terminal UI state. Check that earlier cards remain present; if a list replaces/virtualizes cards, accumulate observed batches with individual evidence instead of assuming append-only behavior. Enforce shared deadline, configurable click/card limits and no-progress detection. A still-present button with no progress, ambiguous container, unexpected navigation or exhausted budget yields partial/unknown, preserving captured evidence. Do not click vehicle consultation buttons. No authentication, private API or paid MCP dependency is required.

Limits must accommodate a bounded long list without hardcoding a historical card or click count. Validate the recipe action and include its behavior/version in relevant configuration fingerprints. Inventory partial captures must not be resumed as complete under an upgraded recipe.

### 3. Extract four offer sections with separate conditions

Extend `adapters/gongcar.py` for popular and promotional layouts on both paths. Preserve legacy table support. Scope shared footnotes strictly to their own section. Rentpass popular deposit percentage, zero-deposit promotion, and longterm prepayment percentage are separate concepts; never convert missing percentages to amounts or assume absent options/term/mileage.

Distinguish current discounted amount, reference/original amount and starting-price qualifier. Determine roles from visible labels and rendered styling/structure; never choose the smallest number as an implicit discount. Ambiguous roles remain unverified. A card with two prices is one offer with separately evidenced price roles, not two vehicles. Collect update text verbatim; do not infer validity from a campaign title alone.

Use observed public source IDs where available. Otherwise create explicitly local observation identities, preserving same-name cards with different prices/conditions and uncertain collisions. Do not present generated hashes as vendor identifiers. Preserve section, DOM occurrence/locator and original evidence; mark cross-run matching uncertain if stable source identity is unavailable.

### 4. Preserve evidence and expose honest results

Save final expanded DOM and screenshot plus per-step card counts, button states, stop reason and section evidence. Preserve public reference prices separately from current prices in SQLite and XLSX. Carry coverage through MCP status/result/report/gaps and web UI using the same backend result, with English/Korean/Japanese labels. Present discovered, extracted, and review counts as distinct fields.

Do not equate job execution succeeded, discovery complete and commercially verified. An ordinary browser fallback must use the same evidence/completeness contract. Explicitly disclose unsupported sections and newly changed layouts.

Implementation files include `pipeline.py`, `catalog_collection.py`, `site_collection.py`, `report.py`, `assistant_workspace.py`, `assistant_server.py`, `web_server.py`, and the relevant `web/` result views and translation dictionaries.

### 5. Validate before another model benchmark

1. Offline parser fixtures: four sections, duplicated vehicle names, differing offers, missing conditions, unknown price roles, reference/current prices, hidden elements and legacy tables.
2. Offline browser tests: bounded multi-step expansion, unrelated More buttons, delayed initial render, delayed append, no progress, timeout, replacement lists and missing/explicit-empty sections. Use synthetic pages, no external network.
3. Integration: checkpoints/resume, config-version changes, SQLite/evidence receipts, XLSX, MCP and UI expose identical counts/states; partial lists never display complete. Preserve review status when commercial conditions are incomplete.
4. Opt-in live run against both category URLs; independently expand and enumerate the same current sections in a browser. Compare offer identity/name, current price, price role, conditions and evidence, not only totals. Inspect promotion carousel/overflow and category selection for hidden offers.
5. Only after collection correctness passes, run a small subscription MCP smoke check, then repeat expensive four-model timing if needed. Separate source-discovery time, collection time and model overhead.

Acceptance: every observed public offer has an evidenced price record or explicit unresolved gap; all discovered sections have defensible completion states; no inferred values, condition leakage, silent deduplication or consultation submissions. No claim of complete private inventory.

## Ongoing checks

For a new site layout, verify list controls, overflow, card identity, commercial conditions, evidence hashes, and XLSX coverage before treating the public section as complete. Store live research results outside Git.
