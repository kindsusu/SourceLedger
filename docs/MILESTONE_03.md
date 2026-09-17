# Milestone 0.3: bounded research execution

[English](MILESTONE_03.md) | [한국어](MILESTONE_03.ko.md)

Objective: reduce manual URL discovery and first-time extraction setup while preserving the exact-evidence validation already available in 0.2.

Scope:

- An explicitly configured SearXNG JSON search endpoint, with bounded requests and candidate provenance.
- Deterministic structured-data/semantic-HTML rule proposals, optionally assisted by a local Ollama model. Models propose selectors only, never price values or executable actions.
- A bounded controller that checkpoints source processing, verifies proposed configs, and optionally activates configs only with matching known samples.
- English interfaces and documentation, with a Korean guide. SQLite remains authoritative and XLSX remains the report format.

Completion: controlled integration tests cover search through source collection, proposals, verification, activation, reporting, resume, budgets, and failure handling. The existing test suite must continue to pass. Live services or target sites not actually exercised will remain explicitly unverified.

Deferred: service installation, paid services, cloud model transport of internal material, arbitrary browser action planning, scheduled operation, and client-specific Claude/ChatGPT UI deployment.
