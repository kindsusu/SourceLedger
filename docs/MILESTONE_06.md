# Milestone 0.4: local assistant connector

## Objective

Make a configured SourceLedger workspace callable from local Codex, Claude
Code, and Claude Desktop installations without requiring a paid MCP service or
silently editing client configuration.

## Delivered scope

- Generate client-specific local stdio MCP snippets with an absolute,
  environment-local Python executable and workspace path.
- Explicit `--install` only: preserve unrelated settings, make a byte backup,
  retain file permissions where supported, and refuse same-name conflicts.
- Use a separate single worker for bounded queued work; expose start, status,
  stop, list, read, and explicit resume actions.
- Keep onboarding/source changes and queued collection operations inside one
  workspace. Paths, commands, external providers, and model calls are bounded
  or rejected.
- Document Codex, Claude Code, and Claude Desktop installation targets without
  claiming a live client UI test.

## Acceptance evidence

Targeted local configuration tests verify generated TOML/JSON, project-local
Claude Code settings, merge preservation, backups, conflict refusal, and
virtual-environment interpreter paths. Runtime tests verify the singleton
worker, persisted jobs, stop behavior, interrupted-job handling, and path
boundaries. The final Windows suite on 2026-09-22 passed **258 tests, with 1
POSIX-only test skipped**, in 101.01 seconds (`PYTHONUTF8=1`,
`SOURCELEDGER_HEADLESS=1`, `.venv/Scripts/python.exe -m pytest tests -q`). The
previous 212-test suite is a 0.3 baseline.

An actual stdio test closes MCP while a local fixture request is in progress,
then reconnects to read the completed worker result and XLSX resource. A
separate wheel installation passed the collection/activation smoke check and
`scripts/smoke_assistant_install.py`: onboarding, persisted queued work after
MCP exit, an independently started worker, observation paging, and byte/hash
verification of the delivered XLSX resource. No live site or paid service was
used. Hosted platform outcomes are available in the repository's CI results.

The installed Codex CLI parsed an isolated generated configuration containing
a Korean/space workspace path. The installed Claude Code CLI parsed the
generated project configuration, subject to its normal client approval.
Claude Desktop settings were JSON-round-tripped. These are configuration
checks, not a live graphical-client connection test.

## Deferred

No hosted server, ChatGPT web/mobile connector, remote file delivery, live
client GUI verification, host-browser session handoff, service registration,
scheduling, paid MCP provider, or autonomous repair is included.
