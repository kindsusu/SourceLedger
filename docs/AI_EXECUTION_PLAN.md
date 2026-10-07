# Dual AI execution plan

## Goal

Support source recommendations through both the local web UI and an AI application's existing MCP connection. A recommendation is an unverified source candidate, never a verified price observation. The operator selects candidates before adding research targets.

## Interaction

1. Keep direct company/site entry and keyword recommendation requests.
2. Add an explicit execution choice: **Run in web UI** or **Use an AI app**.
3. Web execution uses an installed, authenticated **Codex CLI** or **Claude Code CLI**. Save a workspace default provider, model and timeout in Connections. Allow provider-default models, available local model suggestions and an explicit model ID. Never imply that this changes the model in an open desktop conversation.
4. Queue web requests and show progress, the requested provider/model, completion or actionable failure. Candidate review stays in Sources. Do not start research automatically or silently fall back to another provider.
5. Preserve the existing MCP request/list/submit flow and copyable handoff. The host AI application controls its own model in this mode.

## Implementation boundaries

- Provider adapter: allowlisted CLI discovery, validated settings, bounded subprocess execution, strict structured results and safe errors. No user-supplied commands, shell execution, API-key storage, automatic installs or login attempts.
- Runtime/API: snapshot the request topic and selected settings at submission, use the durable worker, and release workspace locks during external execution. Reject stale results after a topic change. Existing collection jobs remain unchanged.
- UI: accessible provider/model settings, distinct web and host-app actions, progress and recovery, preserved edits during polling and mobile layouts.
- Documentation: English and Korean setup and usage, prerequisites, model selection boundaries and CLI-account usage implications.

## Execution controls

Web execution is opt-in per request. Run each CLI in an isolated temporary directory with its customization/MCP hooks disabled and only the capabilities needed for source discovery. Preserve managed provider restrictions and native authentication. Do not bypass permission prompts or expose raw provider logs/secrets in errors. Bound elapsed time, captured output and candidate count; terminate owned subprocesses on timeout. Validate all candidates through the existing store before persistence. Search evidence remains unverified until normal collection and validation.

## Ownership and completion criteria

- A Sol worker implements provider/settings adapters and focused offline tests.
- A Sol worker implements web UI interactions and focused browser checks.
- A Luna worker updates the English and Korean usage documentation. The orchestrator implements runtime/API integration, reviews all changes and verifies the complete flow. Workers do not redelegate or overwrite others' edits.
- Complete when both execution paths work against the same request store, selected models reach the provider command, errors are visible, unrelated polling remains responsive during generation, and no candidate is automatically collected or promoted to a price observation.
- Test provider contracts with synthetic subprocesses; test API/worker/UI integration without external services. Run the relevant full suite. Clearly distinguish offline verification from any live authenticated model run.

## Implementation and verification

- Implemented local CLI settings, model suggestions/custom IDs, background recommendation jobs and the existing host-app MCP handoff. The provider/model and request inputs are pinned when queued. Job receipts identify returned candidates; observations are unchanged.
- Added duplicate-run protection, stale-topic rejection, bounded process trees, safe errors and explicit retries. Recommendation retries start a new invocation from Sources; they are not collection checkpoints.
- Verified real browser → HTTP API → durable worker → recommendation store → manual source selection with a synthetic model response. Verified the existing browser/MCP handoff, saved settings, failures, retries and layouts from 375 to 1440 pixels.
- The Windows full suite passed with `SOURCELEDGER_HEADLESS=1`: 310 passed, 1 skipped. Subsequent retry handling has focused regression coverage. GitHub CI runs tests on Windows, macOS and Linux and checks an installed distribution.
- Installed Codex CLI 0.159.2 and Claude Code 2.1.284 passed local option/version preflight. No authenticated model inference or real research collection was performed. CLI account permissions, model availability and live search results are checked on an explicitly requested run.
