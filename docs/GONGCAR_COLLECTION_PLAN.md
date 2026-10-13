# Gongcar full catalog collection plan

Status: implemented and live-verified on 2026-10-12. The original proposal below is retained as historical context.

## Implementation outcome

The final live run collected **88 public offers and 88 current prices** in **19.12 seconds**, including SQLite and XLSX generation: rentpass popular 18, rentpass promotion 3, longterm popular 64, longterm promotion 3. An independent interactive browser expanded both popular lists to button disappearance (4 and 15 clicks) and matched every section's name/current-price multiset. Promotions had three DOM cards each and no overflow or additional pagination control. Reference prices were separately retained for all three longterm promotions.

The earlier 121-card observation below is historical, not a fixed acceptance target. The live catalog changed; this run does not establish the cause. Completeness applies only to the four observed public sections, not private inventory, daily/monthly rental, all configurable quotes, or contract verification. Offers retain review status where commercial conditions are incomplete.

Implemented bounded expansion, image-independent card extraction, local offer identities without silent same-name/source-ID deduplication, scoped deposit/prepayment conditions, explicit partial/unknown section states, SQLite coverage persistence, XLSX regeneration, and MCP/UI coverage presentation. Final DOM, screenshots and receipts remain in the ignored local research output. No paid/model API was used for collection.

Validation: full offline suite 424 passed / 1 skipped with `SOURCELEDGER_HEADLESS=1`; subsequent focused regression checks cover final parser and configuration-roundtrip fixes. A synthetic 97-card / 24-click expansion also passes. This machine's managed headful Chromium failed at process launch (`spawn UNKNOWN`); headless collection and independent in-app browsing succeeded. Use the documented headless setting here; headful runtime repair remains an environment issue.

Final local run ID: `abaacf7285fd4819aeff9da78a170f5f`. Re-exporting its saved configuration preserved the four-section coverage sheet. Research artifacts are intentionally not committed.

Final regression note: 81 related tests passed; the 97-card fixture exceeded its test-only 10-second deadline and correctly returned partial at 76 cards. Its completion test now uses the production 120-second budget while separate deadline tests remain unchanged. A subsequent browser-test rerun encountered Chromium launch unavailability and skipped browser cases; this last environment-limited rerun is not evidence of an all-green final browser suite. Earlier full-suite and live-run results above are recorded separately.

## Scope and verification

Collect publicly displayed offers from `/rentpass` and `/longterm`. Monthly rental, daily rental, private inventory and submitted consultation quotes are outside this plan. An offer is not necessarily a unique vehicle model or a verified contractual quote.

Fresh interactive browser verification found:

| Section | Observed offers | Completion evidence |
|---|---:|---|
| Rentpass popular | 18 | 4 → 8 → 12 → 16 → 18; scoped More button disappeared |
| Rentpass promotion | 3 | Three displayed promotional cards |
| Longterm popular | 97 | Repeated More expansion; final 92 → 96 → 97; button disappeared; 97 distinct card texts |
| Longterm promotion | 3 | Three displayed cards, each with original and discounted monthly amounts |

The current observed total is 121 offer cards, not the previously reported 68. Earlier inventory recorded 43 longterm popular cards and four promotions. Current evidence cannot establish whether this difference is historical undercounting or a site change. Treat the old count as unsuitable for a completeness target. Do not hardcode either number. Promotions may require carousel/overflow inspection before asserting section completeness.

The longterm heading rendered before its cards; a page or heading-ready condition is insufficient. Multiple unrelated More buttons exist. A broad ancestor section locator matched twice; the implementation must identify one unambiguous product container, not blindly use the first global button. Browser inspection expanded the list but did not submit forms. One long inspection call exceeded its tool deadline; inspection resumed from the existing page, without reloading or claiming that timeout was a site failure.

Existing offline checks: `python -m pytest tests/test_site_adapters.py tests/test_site_collection.py tests/test_catalog_collection.py tests/test_incremental.py -q`: **60 passed in 13.41s**. Tests passed after rerunning outside sandbox file/registry restrictions. These tests validate current supported behavior, not complete live catalogs. The rentpass fixture contains three synthetic popular cards, not a production three-card limit.

## Confirmed implementation gaps

- `site_collection._ready_recipe` only waits for visible cards/table cells; no expansion exists.
- `collectors.py` supports fixed click/select/fill/wait/assert steps, without a bounded repeat-until-complete operation.
- `adapters/gongcar.py` routes rentpass exclusively to popular-card extraction and deliberately excludes promotions. Longterm falls through to legacy table parsing and cannot extract its current cards.
- Page-level visited/no_data results and zero-item coverage gaps cannot detect a partially collected section.
- Current popular-card identity uses name/options/conditions. Longterm can show repeated names without option text. Reusing that identity design could silently collapse distinct offers.
- Playwright captures are not reused by the incremental snapshot cache. Existing final DOM/screenshot/receipt handling can be reused; list-completion metadata must additionally survive checkpoint/resume and exports.

## Implementation sequence

### 1. Define completeness independently from price verification

Add a backward-compatible section result containing section key, discovery status, observed card count, extracted offer count, numeric current-price count, unparsed count, expansion status, termination reason, click count and evidence reference. Use explicit unknown/partial/complete states. A missing heading is not proof of an empty section; require an explicit empty state or return unknown/layout_changed.

Do not infer expected inventory from past runs. List completion only means the observed public section ended. Price conditions may remain review-only after discovery completes. Capture unsupported/malformed cards as gaps without synthesizing numbers. Separate page count, offer count and price-value count.

Owners/files: root integration in `models.py`, `catalog_collection.py`, `site_collection.py`, `assistant_workspace.py` and storage/checkpoint serialization.

### 2. Expand public lists through bounded browser interaction

Implement a reusable, validated expansion primitive in `collectors.py`, configured through a Gongcar page-specific recipe in `site_collection.py`. Require one scoped list container and one scoped More control. Wait for actual cards or a confirmed empty state before testing completion. Select the All category only when necessary and record selected state.

After each click, wait for new card identities/content or a confirmed terminal UI state. Check that earlier cards remain present; if a list replaces/virtualizes cards, accumulate observed batches with individual evidence instead of assuming append-only behavior. Enforce shared deadline, configurable click/card limits and no-progress detection. A still-present button with no progress, ambiguous container, unexpected navigation or exhausted budget yields partial/unknown, preserving captured evidence. Do not click vehicle consultation buttons. No authentication, private API or paid MCP dependency is required.

Limits must support the observed 24 expansion clicks for 97 cards, but must not hardcode 24 or 97. Validate the new recipe action and include its behavior/version in relevant configuration fingerprints. Inventory partial captures must not be resumed as complete under an upgraded recipe.

### 3. Extract four offer sections with separate conditions

Extend `adapters/gongcar.py` for popular and promotional layouts on both paths. Preserve legacy table support. Scope shared footnotes strictly to their own section. Rentpass popular deposit percentage, zero-deposit promotion, and longterm prepayment percentage are separate concepts; never convert missing percentages to amounts or assume absent options/term/mileage.

Distinguish current discounted amount, reference/original amount and starting-price qualifier. Determine roles from visible labels and rendered styling/structure; never choose the smallest number as an implicit discount. Ambiguous roles remain unverified. A card with two prices is one offer with separately evidenced price roles, not two vehicles. Collect update text verbatim; do not infer validity from a campaign title alone.

Use observed public source IDs where available. Otherwise create explicitly local observation identities, preserving same-name cards with different prices/conditions and uncertain collisions. Do not present generated hashes as vendor identifiers. Preserve section, DOM occurrence/locator and original evidence; mark cross-run matching uncertain if stable source identity is unavailable.

### 4. Preserve evidence and expose honest results

Save final expanded DOM and screenshot plus per-step card counts, button states, stop reason and section evidence. Preserve public reference prices separately from current prices in SQLite and XLSX. Carry coverage through MCP status/result/report/gaps and web UI using the same backend result, with English/Korean/Japanese labels. Example: `97 offers discovered; 96 current prices extracted; 1 needs review; list expansion complete`.

Do not equate job execution succeeded, discovery complete and commercially verified. An ordinary browser fallback must use the same evidence/completeness contract. Explicitly disclose unsupported sections and newly changed layouts.

Owners/files: `pipeline.py`, `catalog_collection.py`, `site_collection.py`, `report.py`, `assistant_workspace.py`, `assistant_server.py`, `web_server.py`, relevant `web/` result views and translation dictionaries. Root owns schema/integration; a bounded Sol worker can own browser expansion, another the four-section adapter, with distinct files and no redelegation. UI/docs follow the settled contract.

### 5. Validate before another model benchmark

1. Offline parser fixtures: four sections, duplicated vehicle names, differing offers, missing conditions, unknown price roles, reference/current prices, hidden elements and legacy tables.
2. Offline browser tests: 4→8→…→97 expansion, unrelated More buttons, delayed initial render, delayed append, no progress, timeout, replacement lists and missing/explicit-empty sections. Use synthetic pages, no external network.
3. Integration: checkpoints/resume, config-version changes, SQLite/evidence receipts, XLSX, MCP and UI expose identical counts/states; partial lists never display complete. Preserve review status when commercial conditions are incomplete.
4. Opt-in live run against both category URLs; independently expand and enumerate the same current sections in a browser. Compare offer identity/name, current price, price role, conditions and evidence, not only totals. Current 121 is a dated observation, not an acceptance constant. Inspect promotion carousel/overflow and category selection for hidden offers.
5. Only after collection correctness passes, run a small subscription MCP smoke check, then repeat expensive four-model timing if needed. Separate source-discovery time, collection time and model overhead.

Acceptance: every observed public offer has an evidenced price record or explicit unresolved gap; all discovered sections have defensible completion states; no inferred values, condition leakage, silent deduplication or consultation submissions. No claim of complete private inventory. Product code is unchanged by this planning task.

## Open items for implementation

Confirm stable public offer identifiers, promotion overflow behavior, discount semantics from rendered evidence, exact data-model extension for original prices/prepayment, and retention of partially expanded page evidence on failure. These are implementation checks, not reasons to assume missing values or to hardcode historical counts.
