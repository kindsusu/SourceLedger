# Bounded autonomy roadmap

[English](ROADMAP.md) | [한국어](ROADMAP.ko.md)

## Target

After initial setup, SourceLedger should discover relevant sources, collect evidence, validate observations, and preserve history with minimal routine intervention. Authentication renewal and genuinely ambiguous data remain explicit exceptions. Missing prices are never guessed.

## Fixed product decisions

- English is the default interface and report language; Korean onboarding and documentation are available. Full Korean workbook localization is not implemented.
- SQLite and source evidence are authoritative. XLSX is the human-readable report. CSV export and ERP integration are outside this scope.
- No paid MCP, search, or model service is enabled silently.
- Search candidates, draft configurations, verification receipts, and verified price observations are distinct objects.

## Stages

1. **First-run research workspace — implemented.** Save industry, one lead product, market, identifiers, and authorized candidates in English or Korean. The regular collection configuration remains separate and supports multiple products.
2. **Bounded source discovery and draft — implemented.** Persist explicit candidates, discover same-host links with HTTP/browser fallback, and optionally query a configured SearXNG server. Search results remain candidates with provenance. Generate drafts only from explicit identifiers and candidates.
3. **Verification and activation — implemented.** Verify exact configuration and retained evidence, optionally check known samples, and activate only an unchanged passing configuration. Local receipts are audit records, not signatures.
4. **Rule proposals and bounded execution — implemented in 0.3.** Structured-data and semantic-HTML proposals, optional local Ollama selectors, checkpointed source processing, fresh combined verification, and optional sample-gated activation. Browser action planning, rule-change repair, and live model evaluation remain pending.
5. **Operations and client delivery — pending.** Add scheduling, restart recovery, bounded model/browser usage, exception notifications, client UI verification, and controlled deployment.

## Acceptance criteria for the next stage

- A configured search provider is explicit, bounded, and records origin and retrieval time; an unconfigured provider makes no network calls.
- Proposed rules cannot alter active collection configuration until exact evidence and, when supplied, known samples pass verification.
- Representative intended sites measure extraction accuracy, access outcomes, and browser fallback behavior before unattended operation is claimed.
- Scheduling and agent actions have explicit time, browser, and model-cost limits with actionable failure records.

Next priority: evaluate representative intended sites and configured providers, then add controlled recipe recovery and operations. A bounded controller is available; unrestricted autonomous operation is not.
