# Evidence and recovery through MCP

Start in a connected AI app. Use its available search/browser tools to propose a research plan, show the scope to the user, and collect only after confirmation. No subordinate models or paid scraping service are required by this workflow. Browser availability and model usage remain controlled by the host app.

After `start_research_plan` finishes:

1. Call `get_research_gaps(job_id, offset=0, limit=50)` to inspect failures, unprocessed pages and unresolved conditions. This does not browse or call a model. A visited page is not necessarily a verified offer.
2. For retryable failures, call `retry_research_pages(job_id, urls, max_seconds=120)`. Only recorded unresolved URLs from the same confirmed revision are accepted. Authentication/policy denials and already extracted review-only prices are not blind-retry targets. This explicitly starts a new budget of one page per URL, up to 50 pages and 600 seconds, and does not discover new links. Completed page captures are reused in a combined report; each attempt has a separate run history.
3. If the host browser can inspect a page, submit literal evidence with `submit_browser_evidence`. Capture the actual product/option block first. Supply `job_id`, an approved coverage `url`, timezone-aware ISO `captured_at`, `page_text`, `fields` and `locator`. `fields.name` is required. Omit absent prices, currencies and terms. Every submitted value must occur verbatim in the supplied text. One submission represents one product/option; different contract durations remain separate.
4. Read `get_job_observations` and `get_job_report`. The latter exposes the registered XLSX resource. Browser submissions appear alongside collected records in SQLite, the web UI's observation list, and the regenerated XLSX, clearly marked for review.

An optional `screenshot_path` accepts a workspace-relative PNG up to 10 MiB. Source text, screenshots and field associations supplied by the host are untrusted evidence. Literal text support does **not** independently prove page authenticity, visibility or association with the right option. Submissions never become verified/comparable automatically. Existing price, date and condition checks still run; unresolved values remain missing or review-only. Identical payloads are idempotent. Browser submissions are preserved as review-only history in later partial retries.

For an **interrupted** research-plan job, use `resume_job_execution(job_id)`. The MCP route starts the worker. SQLite checkpoints retain completed page captures, the queue, the approved snapshot and consumed budgets. Completed pages are not fetched again. If a process dies during a fetch, its reserved page/time budget is conservatively charged and the page is listed as interrupted for an explicit retry. Time/page exhaustion is not reset by resuming. AI preview jobs and older plan jobs without checkpoints must be started anew.

The web UI and MCP must use the same workspace to show the same evidence. MCP does not wake a conversation in another app. Browser evidence submission is an MCP action, not a new UI upload form. Host-browser interaction is performed by the connected AI, not by an automatic background browser agent.

Coverage remains limited to selected/visited pages. Jetcar discovery follows vehicle detail links rather than login, terms or company-navigation links. This is not a claim that every vehicle or option on the site has been collected.

## Clear results and supporting evidence

`get_job_observations.rows` now contains collection results, not an extra price row for each browser capture. Each result includes `field_status`, `missing_conditions`, `supporting_evidence` and `conflicts`. Result and evidence counts are separate. A unique matching source URL, product name and supplied option identifiers (including contract term) can associate a capture; ambiguous or mismatched captures remain in `evidence_submissions`. Association does not verify a price. Differing supported prices remain visible and disable direct comparison in the result view. Source values are never overwritten or filled from a supplemental capture.

The first XLSX sheet, **Results**, shows prices, recorded conditions, missing conditions and supporting-evidence counts. **Supporting Evidence** identifies the linked observation, or shows why a capture is unlinked. **Rental Quotes** contains collection results only. **Observation History** and **Source Evidence** retain every original record for audit. The web UI shows the same result counts, expandable evidence and per-field recorded/missing/conflict labels in English, Korean and Japanese. “Recorded” means a value was collected, not that a final commercial quote was verified.
