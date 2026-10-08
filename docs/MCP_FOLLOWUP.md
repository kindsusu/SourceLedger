# Evidence and recovery through MCP

Start in a connected AI app. Use its available search/browser tools to propose a research plan, show the scope to the user, and collect only after confirmation. No subordinate models or paid scraping service are required by this workflow. Browser availability and model usage remain controlled by the host app.

`start_research_plan` collects only selected URLs by default (`follow_links=false`). Enable `follow_links=true` only for user-approved bounded same-host discovery. Product lists belong to their respective company/source; do not apply one company's requested models to every selected source.

Use `wait_for_job(job_id, timeout_seconds=20)` after starting collection. Each call waits asynchronously for at most 30 seconds and returns compact status. If it is still running, wait again; a timeout is not a failed job. Successful execution does not establish price completeness.

After collection finishes:

1. Call `get_research_gaps(job_id, offset=0, limit=50)` to inspect failures, unprocessed pages and unresolved conditions. This does not browse or call a model. A visited page is not necessarily a verified offer.
2. For retryable failures, call `retry_research_pages(job_id, urls, max_seconds=120)`. Only recorded unresolved URLs from the same confirmed revision are accepted. Authentication/policy denials and already extracted review-only prices are not blind-retry targets. This explicitly starts a new budget of one page per URL, up to 50 pages and 600 seconds, and does not discover new links. Completed page captures are reused in a combined report; each attempt has a separate run history.
3. If the host browser can inspect a page, submit literal evidence with `submit_browser_evidence`. Capture the actual product/option block first. Supply `job_id`, an approved coverage `url`, timezone-aware ISO `captured_at`, `page_text`, `fields` and `locator`. `fields.name` is required. Omit absent prices, currencies and terms. Every submitted value must occur verbatim in the supplied text. One submission represents one product/option; different contract durations remain separate.
4. Read `get_job_observations` and `get_job_report`. The latter exposes the registered XLSX resource. Browser submissions appear alongside collected records in SQLite, the web UI's observation list, and the regenerated XLSX, clearly marked for review.

An optional `screenshot_path` accepts a workspace-relative PNG up to 10 MiB. Source text, screenshots and field associations supplied by the host are untrusted evidence. Literal text support does **not** independently prove page authenticity, visibility or association with the right option. Submissions never become verified/comparable automatically. Existing price, date and condition checks still run; unresolved values remain missing or review-only. Identical payloads are idempotent. Browser submissions are preserved as review-only history in later partial retries.

For an **interrupted** research-plan job, use `resume_job_execution(job_id)`. The MCP route starts the worker. SQLite checkpoints retain completed page captures, the queue, the approved snapshot and consumed budgets. Completed pages are not fetched again. If a process dies during a fetch, its reserved page/time budget is conservatively charged and the page is listed as interrupted for an explicit retry. Time/page exhaustion is not reset by resuming. AI preview jobs and older plan jobs without checkpoints must be started anew.

The web UI and MCP must use the same workspace to show the same evidence. MCP does not wake a conversation in another app. Browser evidence submission is an MCP action, not a new UI upload form. Host-browser interaction is performed by the connected AI, not by an automatic background browser agent.

Coverage remains limited to selected/visited pages. Jetcar discovery follows vehicle detail links rather than login, terms or company-navigation links. This is not a claim that every vehicle or option on the site has been collected.

## Clear results and supporting evidence

`get_job_observations` returns compact rows by default. Use `offset`/`limit` for results and `evidence_offset`/`evidence_limit` for separate submissions (default 20, maximum 50 per MCP page). Request `detail=true` only when original evidence details are needed. The web UI retains detailed result rows.

A unique matching source URL, product name and supplied option identifiers (including contract term) can associate a browser capture with an existing result without adding a duplicate price row. Unmatched price candidates can appear in Results with review status; ambiguous matches remain separate evidence. Captures are retained in `evidence_submissions` and audit sheets. Association never verifies a price, and conflicts disable comparison. Source values are never overwritten from a supplemental capture.

Submit literal values with canonical keys: `name`, `price`, `currency`, `price_basis`, `term_months`, `deposit_amount`, `deposit_percent`, `annual_mileage_km`, `options`, `insurance`, and `tax`, only when present. Keep `월 290,000원~` intact instead of converting it in the submission. Supported numeric interpretations are derived separately, preserving the starting-price qualifier and original text. “Missing” means not recorded or unconfirmed, not necessarily absent from the source page.

The first XLSX sheet, **Results**, shows prices, recorded conditions, missing conditions and supporting-evidence counts, including eligible browser candidates clearly marked for review. **Supporting Evidence** identifies the linked observation, or shows why a capture is unlinked. **Observation History** and **Source Evidence** retain every original record for audit. The web UI shows result counts, source price text and review notices in English, Korean and Japanese. “Recorded” means a value was collected, not that a final commercial quote was verified.
