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

Use offline regression fixtures and replay retained captures to check section coverage, issue handling, and evidence association. Report incomplete pages as review items. Replay is not a fresh site visit, so it cannot establish current inventory or collection speed. Run the affected browser, UI, follow-up, issue, pipeline, and redirect tests after behavior changes.

## Remaining operational boundaries

Unknown sites still require inspection and a reviewed recipe. Arbitrary custom widgets, virtualized lists and missing inventory boundaries cannot be declared complete. A visible ready indicator does not prove asynchronous price settlement; option candidates retain a review flag. CAPTCHAs, login and authorized internal access can require operator action. The connected AI application's browser is not silently controlled by SourceLedger.

Browser tests used headless mode. The previously observed visible-browser launch failure is an environment limitation, not demonstrated fixed by these tests. No new production-site completeness benchmark, model speed comparison or token-usage measurement was performed. See [collection limits and recovery](LIMITATION_MANAGEMENT.md) for the supported workflow and evidence requirements.
