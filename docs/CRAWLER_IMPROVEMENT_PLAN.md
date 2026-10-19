# Upstream-inspired crawler implementation plan

Scope: apply the Exa MCP / Scrapy architecture review, including the completed checkpoint storage optimization. Keep subscription-app orchestration, approved research scope, source evidence, SQLite and XLSX. No paid service activation or model API calls.

1. Implement a bounded collection queue with durable URL states, conservative restart accounting, limited concurrency, per-host pacing, transient retry backoff, and a lower browser limit. The coordinator owns all checkpoint and final ledger writes.
2. Carry concurrency settings through execution preview, web UI, MCP, pinned jobs, and resume scope. Explain actual execution before starting; preserve Sol/Opus as recommendations rather than changing host models.
3. Provide compact MCP progress counts and bounded result paging, with no raw page content in routine polling.
4. Accept optional externally discovered Exa candidates through the connected AI app. Preserve provenance and truncation information; never promote suggestions or fetched text to verified price observations. No direct Exa subscription or API is required by SourceLedger.
5. Verify identical frozen inputs, evidence/price parity, concurrency limits, interrupted recovery, retries, and output size using offline fixtures and existing captures. Distinguish local replay timing from live end-to-end speed.

Completion requires UI/MCP integration, meaningful tests, documentation of measured benefits and remaining limits. Existing research datasets are not overwritten and repository changes are not automatically pushed.
