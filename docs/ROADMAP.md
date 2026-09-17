# Bounded autonomy roadmap

[English](ROADMAP.md) | [한국어](ROADMAP.ko.md)

## Target

After initial setup, SourceLedger should discover relevant sources, collect evidence, validate observations, and preserve history with minimal routine intervention. Authentication renewal and genuinely ambiguous data are explicit exceptions. Missing prices are never guessed.

## Fixed product decisions

- Ask for industry, product, and market at first execution. No analysis-purpose field.
- English is the default interface/documentation language; Korean is also supported.
- SQLite and source evidence are authoritative. XLSX is the human-readable report. No CSV export or ERP integration in this scope.
- No paid MCP, search, or model service is silently enabled.
- Search results, proposed extraction rules, and verified price observations are different objects.

## Implementation sequence

1. **First-run research workspace:** persist the operator's topic, identifiers when known, source candidates, and next actions without changing the existing exact collection configuration.
2. **Bounded source discovery:** collect candidates from explicitly configured seeds and an optional search-provider connection. Preserve origin and retrieval time; never treat snippets as prices.
3. **Recipe validation and activation:** propose a collection configuration, test it against source evidence and known product facts, and activate only the exact version that passed. Rule or product changes invalidate the result.
4. **Adaptive collection:** add a model-backed planning loop for previously unseen layouts and broken recipes. Save successful rules for normal code-driven collection on later runs.
5. **Operations:** scheduling, restart recovery, bounded model/browser usage, and exception-only notifications.

## Acceptance criteria

- Initial configuration can be created in English or Korean without analysis-purpose input or secret values.
- An unconfigured search/model provider is reported honestly and makes no hidden network calls.
- Candidate discovery cannot modify active collection rules or produce verified prices.
- Invalid identifiers, mismatched samples, missing evidence, and modified recipes cannot be activated.
- The existing collection, provenance, resume, and XLSX tests continue to pass.
- Controlled integration tests cover the actual collection flow. Real-site success is reported only after testing the intended sites.

This roadmap describes future milestones, not a claim that all autonomous behavior is already available.
