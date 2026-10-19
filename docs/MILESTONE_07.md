# Milestone 07: Local browser workspace

Provide a browser interface for first-run setup, source management, bounded jobs,
observations, XLSX reports, and local assistant connection settings. The browser
and MCP share the existing workspace and independent worker.

## Scope and design

- English interface and English/Korean instructions. Ask for industry, product,
  and market on first use; preserve exact identifiers and source evidence.
- Overview, Sources, Runs, and Connections views. Empty and incomplete states
  remain explicit. Execution success does not imply verified prices.
- Bundle HTML, CSS, JavaScript, and SVG assets in the Python wheel. No Node build,
  CDN, external font, paid model, or MCP service is required to open the UI.
- Use the UI/UX Pro Max dashboard and accessibility guidance, with SourceLedger's
  navy/mint palette, visible focus, labeled controls, and responsive navigation.
- Bind only to loopback. Validate Host, Origin, request size, and per-server CSRF
  tokens. Downloads resolve only through registered job reports.
- Generate reviewable MCP snippets; do not change a client's configuration from
  the browser. Clients and browser must use the same workspace root.
- Keep SQLite and evidence authoritative, XLSX for reports. CSV export, ERP,
  remote hosting, arbitrary third-party MCP clients, and live price collection
  are outside this change.

## Acceptance checks

1. A fresh browser session completes onboarding and registers explicit sources.
2. Browser and MCP can inspect the same persisted jobs and results.
3. A bounded local fixture job exposes observations and a valid XLSX download.
4. Form input and keyboard focus survive polling; mobile layout stays usable.
5. Cross-origin mutations, invalid input, and arbitrary file access are rejected.
6. An installed wheel serves every asset outside the checkout.
7. Existing offline tests and the new UI/API checks pass.

## Validation (Windows, 2026-09-23)

- Full suite: **272 passed, 1 skipped in 125.21s**, with `PYTHONUTF8=1` and
  `SOURCELEDGER_HEADLESS=1`. The skipped case is POSIX virtualenv symlink behavior.
- Final browser/API checks after interaction refinements: **13 passed**.
  Chromium exercised onboarding, escaped source text, focus across polling,
  375/768/1024/1440px layouts, a synthetic file-based job, XLSX download,
  all three MCP snippets, and missing/estimated amount separation.
- Interactive browser review exercised Overview, Sources, Runs, and Connections.
  No page or console errors were observed in the automated acceptance flow.
- The wheel was installed outside the checkout and exercised through actual
  local HTTP requests: bundled assets, onboarding, independent execution,
  observations, XLSX, shared MCP job lookup, and three generated client settings.
- A separate core-only environment ran the same UI flow without MCP installed.
- JavaScript syntax and `git diff --check` passed.

The fixtures contain synthetic prices. No live market collection, paid service,
or Codex/Claude GUI connection was used for these checks. The CI package job now
runs `scripts/smoke_web_install.py` after wheel installation.
