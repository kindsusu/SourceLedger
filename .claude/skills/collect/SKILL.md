---
name: collect
description: Collect prices for an operator-approved URL list with SourceLedger, either through the model-free collect-plan CLI or the sourceledger MCP tools. Use when the user asks to collect, rerun or schedule a SourceLedger URL inventory.
argument-hint: "[path to URL list file]"
---

# SourceLedger collection procedure

Never retype or paste URL lists. Always pass the file path. A succeeded job is not verified evidence.

## 1. Gather inputs

- URL list file: `.txt` (one URL per line), `.csv` (a `url` column) or `.json` (list or `{"urls": [...]}`).
- Topic: industry, product and geographic market. Ask if any is missing; never guess.
- Workspace root (default `.sourceledger`) and optional existing plan ID.

## 2. Choose the route

- **Unattended or scheduled runs (no model):** prepare this command and show it to the user:

  ```powershell
  .\source-ledger.cmd collect-plan --workspace-root <W> --urls-file <F> `
    --industry "<industry>" --product "<product>" --market "<market>" [--plan-id <P>] --yes --json
  ```

  `--yes` approves collecting every URL in the file. Run it only after the user explicitly approves in this
  conversation. Exit code 0 means the job succeeded, 1 failed/interrupted, 2 refused or invalid input,
  3 still running at the wait timeout (`assistant wait --job-id <J>` continues waiting).

- **Interactive through MCP:**
  1. `create_research_plan` with the full request, or reuse a plan from `list_research_plans`.
  2. `import_plan_urls(plan_id, expected_revision, path)`; report `count`, `invalid`, `duplicates`,
     `first_urls`, `last_urls`, `import.file_url_fingerprint` and `plan.url_fingerprint`. Resolve invalid lines
     with the user by fixing the file. For the MCP path the URL file must be inside the workspace; copy it
     there first (`collect-plan --urls-file` accepts any path).
  3. `update_research_plan` for the topic only, then `get_research_batch_execution_plan`.

## 3. Approval (required)

Show: plan ID and revision, URL count, `plan.url_fingerprint` (and `import.file_url_fingerprint`), first/last URLs, host counts, batch size,
concurrency, browser concurrency and per-batch time limit. Ask the user to approve this exact revision.
Permission to preview is not permission to collect.

## 4. Run

After approval: `confirm_research_plan(user_confirmed=true)`, then `start_research_batch_run`, then repeat
`wait_for_job(job_id, timeout_seconds=120)` until completed. The `sourceledger-collector` subagent can do
this step and return a short summary.

## 5. Report

Use `get_research_batch_progress`, `get_research_batch_reports` and `get_research_gaps` (all compact).
Report status, evidence_status, coverage and observation counts, change counts, gaps, attention items and
the final XLSX/dashboard paths. Offer `retry_research_pages` (without `urls`) only for pages the job lists as
retryable. Never claim prices are verified.
