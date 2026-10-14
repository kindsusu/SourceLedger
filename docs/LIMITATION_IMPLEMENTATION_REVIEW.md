# Collection-limit implementation review — 2026-10-14

[한국어](LIMITATION_IMPLEMENTATION_REVIEW.ko.md)

## Implemented behavior

- Reviewed URL-specific rules are revision-pinned in research plans, exposed through MCP and the advanced web editor, and included in the execution preview. A rule change invalidates confirmation.
- Bounded native option combinations, reviewed non-submit option buttons, pagination, and append-only scrolling preserve individual HTML captures. Link discovery includes earlier pagination states. Dependent controls are reread; ambiguous, stalled, replaced, or budget-limited states do not establish whole-site completeness.
- Observations link to the corresponding capture, timestamp, selected label/value and content hash. Failed collections keep earlier captures and receipts as evidence without manufacturing observation rows. The issue report links to this evidence.
- Authorized existing workspace browser profiles are explicit settings. Authentication, browser environment, access policy, temporary failure, exhausted limits and unknown enumeration have separate next actions. Retries are bounded and checked against both job output and stored coverage.
- SQLite tracks issues and append-only status changes. Later matching coverage is required for resolution; reviewed recipe/plan changes require an explicit fingerprint transition. Historical coverage remains unchanged.
- Completed research and batch jobs expose the issue ledger through MCP and the English/Korean/Japanese UI. XLSX issue status refreshes after triage and during report regeneration. A locked workbook reports a pending refresh while retaining the committed SQLite update.

## Validation

The full headless suite initially reported **571 passed, 2 failed, 1 skipped** in 569.34 seconds. It exposed an unsupported issue-API request from ordinary jobs and a stale retry-test error expectation. Both were corrected. Subsequent affected integration suites passed **93 tests** and **23 tests**; these overlap and must not be summed as a unique test count. Final browser and follow-up validation is recorded below.

The retained 20-page Jetcar/Gongcar corpus was reanalyzed with **181 observations**, including **15 cargo-term observations**. This used stored source evidence and no live site or model API requests. The frozen page scope had 20 complete page-coverage checks; the overall result remained `needs_review` because scope/price verification is separate. This is not a claim about today's inventory or collection speed.

Final affected browser/UI/follow-up/issue/pipeline/redirect tests: **63 passed** in 151.06 seconds. This includes both previously failing test paths. These tests overlap the earlier suites. The entire suite was not repeated after the final fixes. The skip in the initial full run was the Windows-inapplicable Unix virtual-environment symlink test.

Python compilation and Git whitespace checks passed. Existing uncommitted changes were preserved. Research evidence, databases and generated reports remain outside committed source assets.

## Remaining operational boundaries

Unknown sites still require inspection and a reviewed recipe. Arbitrary custom widgets, virtualized lists and missing inventory boundaries cannot be declared complete. A visible ready indicator does not prove asynchronous price settlement; option candidates retain a review flag. CAPTCHAs, login and authorized internal access can require operator action. The connected AI application's browser is not silently controlled by SourceLedger.

Browser tests used headless mode. The previously observed visible-browser launch failure is an environment limitation, not demonstrated fixed by these tests. No new production-site completeness benchmark, model speed comparison or token-usage measurement was performed. See [collection limits and recovery](LIMITATION_MANAGEMENT.md) for the supported workflow and evidence requirements.
