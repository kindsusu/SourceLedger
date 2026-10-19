# Price collection reliability

## Goal

Integrate lessons from the supervised rental investigation into SourceLedger's
normal collection, SQLite history, and XLSX reporting. Keep generic unit-price
collection compatible and keep third-party services optional.

## Scope and ownership

- Pricing profiles: distinguish observed prices from calculator estimates,
  preserve rental terms, and exclude hidden or incomparable quotes.
- Reusable site adapters: deterministic, versioned extraction for the supported
  Jetcar, Gongcar, and Funrent page formats. Website scripts are data and are
  never executed by an extractor.
- Access diagnostics: explain domain, DNS, authentication, blocking, and
  rate-limit failures without exposing secrets or weakening URL policy.
- Orchestration: reuse unchanged extraction only after fresh retrieval or a
  validated conditional response, capture browser selection evidence, and
  provide a direct supported-site collection entry point.
- Integration: expose quote conditions and estimates clearly in reports, cache
  repeated evidence validation within a check, and document actual limitations.

## Completion criteria

1. Unknown prices and terms remain unknown. Estimates cannot become observed
   prices or silently enter unit-price comparisons.
2. Offline tests cover the three adapter formats, rental conditions, hidden
   data, access errors, conditional refresh, cache invalidation, and reports.
3. A controlled collection reaches SQLite and XLSX through the normal pipeline.
4. English and Korean usage instructions reflect implemented behavior. Research
   documents, databases, browser profiles, and generated reports stay untracked.

## Boundaries

No CSV, ERP, paid MCP, arbitrary browser-script execution, unattended scheduling,
or unsupported claims of complete live-site coverage. A changed page structure
must produce an explicit review result rather than fabricated observations.

## Verification and remaining work

Final Windows suite on 2026-09-22: **172 passed** with
`.venv/Scripts/python.exe -m pytest tests -q`.

The offline suite covers adapters, estimated/observed value separation, missing
and zero conditions, browser state capture, address classification, cache
invalidation, run-bound evidence re-extraction, and XLSX output. A local Chrome
test checks selected controls, hidden content, and snapshot sanitization.
The generated report was reopened and its changed views were rendered.

Live-site checks and collected examples belong in ignored local research workspaces. Regression fixtures verify term-specific prices, calculator estimates, missing selections, rendered visibility, and address classification. These tests do not establish whole-site completeness.

Remaining work includes bounded listing/pagination traversal, selected-option
enumeration, and a reviewed handoff from a host application's browser when the
program's own browser cannot render a page. The current code does not silently
import host-browser observations or activate unsupported configurations.
