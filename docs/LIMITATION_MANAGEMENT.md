# Collection limits and recovery

[한국어](LIMITATION_MANAGEMENT.ko.md)

Collection now distinguishes reading a page, enumerating its reviewed controls, and verifying an observation. None of those alone proves entire-site coverage.

## Reviewed collection rules

Use `update_research_plan` (or the web UI's advanced collection rules) to add `changes.collection_rules`. Keys must be exact plan candidate URLs. A rule can contain `recipe`, field `selectors`, an optional `row_selector`, and an optional workspace-relative `profile_dir`. Every edit invalidates confirmation. Review the revised execution plan before confirming it. The collector itself does not call a model or paid API.

```json
{
  "collection_rules": {
    "https://shop.example/product": {
      "selectors": {"name": "h1", "price": "#price", "currency": "#currency"},
      "recipe": [{
        "action": "enumerate_options",
        "selectors": ["#size", "#color"],
        "ready_selector": "#price[data-ready='true']",
        "max_variants": 50,
        "progress_wait_seconds": 1
      }]
    }
  }
}
```

Selectors and readiness signals must be checked against the real site. A price selector must read a price shown by the source; never set a guessed currency or commercial condition. Dependent options are reread after their parent changes. Capture limits and unsupported controls remain partial/unknown. Each captured state retains its own HTML, selected labels, capture time, and source hash in SQLite/XLSX.

`paginate` accepts `selector`, `item_selector`, `next_selector`, and `max_pages`. `expand_list` handles reviewed more/scroll controls. Missing controls, stalls, repeated pages, and exhausted budgets are distinct outcomes; unchanged item counts alone cannot prove a whole site is complete.

`scroll_list` supports bounded append-only lists and an explicit `end_selector`; a replaced or virtualized list remains partial. `enumerate_buttons` supports one reviewed group of non-submit buttons with `data-value`. Earlier pagination captures also participate in same-host link discovery. Selected-state counts are not unique-product counts. A visible readiness selector alone cannot prove that an asynchronous price has settled, so option-derived candidates retain a review flag.

## Access and recovery

- An authorized browser profile is opt-in, must already exist inside the workspace, and is not shared through Git. The application does not obtain credentials or bypass challenges.
- A connected Codex/Claude browser may inspect an approved unresolved page and call `submit_browser_evidence`. Literal source excerpts remain review-only. SourceLedger does not silently control an AI app's browser.
- `get_collection_issues` reads the SQLite issue ledger. `triage_collection_issue` records review progress; resolving an issue requires matching evidence or a valid later run. Triage cannot turn historical coverage or a price into a verified value.
- To resolve a coverage issue, use `triage_collection_issue` with `status: "resolved"` and evidence `{"kind":"revalidated_run","reference":"<later-run-id>"}`. The later run must cover the same URL and source, the same adapter (including a missing adapter for generic selectors), and the same completed section. An option issue also requires its locator in that section. If the recipe or confirmed plan fingerprint changed, add `"scope_revision":{"reason":"<reviewed explanation>","changed":{"recipe_fingerprint":{"from":"<old>","to":"<new>"}}}`; include `plan_fingerprint` in `changed` when it changed too. Values must exactly match the recorded runs. This explicitly links a reviewed revision; it does not infer equivalence from a count.
- `retry_research_pages` accepts only eligible unresolved URLs and a bounded new budget. Auth/policy issues require action first. The retry chain is capped; stopping and resuming the same job does not reset its spent budget.
- Completed captures can be reanalyzed when extraction rules change. Fresh collection is needed to check today's prices. Source-structure changes are review signals, not inferred inventory deletions.

Browser startup is diagnosed separately using `source-ledger doctor --verify-browser-launch --browser-display-mode headless` or `visible`. The requested display mode is never silently changed.

The UI lists issues and their next actions; XLSX includes **Collection Issues**. Triage refreshes that worksheet while leaving historical coverage intact. If Excel holds the report open, the triage response says `report_refresh: "pending"`; the SQLite status is already saved. Repeat the same triage action after closing the workbook to refresh it. SQLite remains the authoritative history. No CSV or ERP integration is added.
