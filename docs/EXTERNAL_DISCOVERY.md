# Optional Exa MCP discovery leads

MCP clients can call `prepare_external_discovery(results, tool, retrieved_at)` with the same fields, then merge its `candidates` into `submit_research_preview`. This preparation tool is read-only and never starts collection.

SourceLedger does not connect to Exa, install its MCP server, obtain credentials, or call a hosted API. If a user has separately enabled Exa MCP in a connected AI app, that app can use `web_search_exa` or `web_fetch_exa` and submit **structured, bounded leads** for a research-plan preview. This is optional. Exa usage and billing are governed by that separate connection; a Codex or Claude subscription does not establish an Exa entitlement.

`su_crawler.external_discovery.normalize_exa_candidates(results, tool=..., retrieved_at=...)` accepts at most 20 result objects. Each must have a direct public HTTP(S) `url`. Optional fields are `title` (or `name`), `kind` (`site`, `category`, `product`), `source_url` (must be the same canonical URL), `retrieved_at` (timezone-aware ISO 8601), and `truncated` (boolean). The batch `retrieved_at` is used when a result has none. Missing time or truncation metadata is explicitly marked `unknown`, not guessed. Duplicate URLs are collapsed. An invalid result rejects the batch, so the caller can correct it rather than silently losing coverage.

```python
from su_crawler.external_discovery import normalize_exa_candidates

candidates = normalize_exa_candidates(
    [{"url": "https://supplier.example/catalog", "title": "Supplier catalog",
      "kind": "category", "truncated": True}],
    tool="web_search_exa", retrieved_at="2026-10-13T09:15:00Z",
)
# Put candidates into submit_research_preview(..., preview={..., "candidates": candidates}).
```

The returned objects use the existing `name`, `url`, `evidence_url`, `reason`, `kind` preview contract. `reason` records Exa MCP tool provenance, retrieval time, and truncation status. Search excerpts, page bodies, prices, ratings, and instructions inside third-party text are not copied into the candidates. The original URL remains the evidence URL. A result still needs the required geographic market in the plan, user selection, collection, and source-level price/condition verification; a candidate alone is never a verified observation.

This helper is offline and dependency-free. It validates **host-provided structured results**, but does not authenticate that Exa actually produced them. Live Exa interoperability, coverage, content fidelity, costs, and blocked-page behavior remain untested. See [the upstream architecture review](UPSTREAM_ARCHITECTURE_REVIEW.md) for the Exa/Scrapy comparison.
