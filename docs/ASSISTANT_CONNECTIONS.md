# Local assistant connections

[English](ASSISTANT_CONNECTIONS.md) | [한국어](ASSISTANT_CONNECTIONS.ko.md)

In 0.5, the [browser UI](WEB_UI.md) can generate these settings from **Connections** and start the worker. The browser and assistant share one workspace root; the stdio connector itself remains independent of the web server. Keep using the CLI below when you want to install a generated setting directly.

SourceLedger can expose one workspace to Codex, Claude Code, or Claude
Desktop through a local stdio MCP server. The connector itself is local: it starts no
web server, does not upload the workspace, and does not enable paid search or
model providers. A connected AI can use its own available search/browser tools;
its plan and tool limits still apply. Separately, the browser UI can explicitly run
source recommendations through an installed Codex CLI or Claude Code CLI. The
collection worker is a separate local process, so closing an MCP client does not
terminate a job already owned by that worker.

This guide describes the generated settings and CLI contract. It does **not**
claim that every Codex or Claude graphical client, cloud session, web/mobile
surface, or browser handoff has been exercised in this project.

## Install and connect

Run the following from the repository directory. On macOS/Linux replace the
two `.cmd` invocations with `bash setup.sh` and `bash source-ledger.sh`.

```bat
setup.cmd --mcp --browser
source-ledger.cmd connect --client codex --install
source-ledger.cmd assistant start --workspace-root .sourceledger
```

Use `--client claude-code` or `--client claude-desktop` for the other clients.
Restart or refresh the client after installing its setting. Then ask the
SourceLedger connection to set up the industry, product, and market, and add
only authorized sources.

`connect` defaults to the `.sourceledger` workspace and server name
`sourceledger`. It uses the repository virtual environment's absolute Python
path, so the client does not rely on the current shell or `PATH`.

## Generated settings and safe installation

Without `--install`, `connect` only writes a reviewable snippet:

```bat
source-ledger.cmd connect --client claude-desktop
```

The default output is one of these files below the workspace:

| Client | Generated file | Default installation target |
| --- | --- | --- |
| Codex | `connections/codex.sourceledger.toml` | `CODEX_HOME/config.toml`, or `~/.codex/config.toml` |
| Claude Code | `connections/claude-code.sourceledger.json` | `.mcp.json` in `--project-dir` or the current directory |
| Claude Desktop | `connections/claude-desktop.sourceledger.json` | Windows: `%APPDATA%/Claude/claude_desktop_config.json`; macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` |

Use `--output DIR` or `--name NAME` to keep more than one reviewed connection.
For a Claude Code project other than this repository, choose it explicitly:

```bat
source-ledger.cmd connect --client claude-code --project-dir "C:\work\my-project" --install
```

To register the server for every Claude Code project instead of one project,
use `--scope user`. SourceLedger then prints the equivalent
`claude mcp add --scope user --env PYTHONUTF8=1 --transport stdio <name> -- <python> -m su_crawler serve-assistant ...`
command (`claude_command.command_line`) and does not run it or edit any user
configuration file; run the printed command yourself:

```bat
source-ledger.cmd connect --client claude-code --scope user
```

`--config-file PATH` overrides a client configuration target and requires
`--install`. This is required for Claude Desktop on operating systems other
than Windows and macOS.

An install merges only a missing `sourceledger` server entry. It preserves
unrelated client settings, creates a byte-for-byte backup beside the target
(`.sourceledger-<id>.bak`), and refuses to replace a same-named server with
different settings. Rename the server or inspect and merge the generated
snippet yourself in that case. The project `.mcp.json` and connection backups
are ignored by Git.

## Product conditions and research region

The optional `conditions` array is shared by UI and MCP. Each entry has `field`, `operator`, and `value`. Fields are `brand`, `manufacturer`, `seller`, `model`, `material`, `condition`, `category`, `name`, and `other`; operators are `equals`, `not_equals`, `contains`, and `not_contains`. For example, `{"field":"condition","operator":"equals","value":"new"}` represents a new-product requirement. Keep unsupported requirements in the full request and an `other` condition, which remains unknown pending review.

Submit or edit conditions with `submit_research_preview` and `update_research_plan`, preserving user edits. Keep only explicit user text filters. An unspecified research region must be clarified before suggesting AI source candidates. Storage withholds AI candidates when `topic.market` is empty. Manual URLs are preserved, but cannot be collected until required topic fields are complete.

Collection results include derived condition assessments and page coverage. Existing job and observation tools expose unresolved checks and evidence for review with a connected assistant. A matched scope check does not verify price display, currency, or commercial terms. Collection adds no automatic per-product model calls.

## Worker and job lifecycle

Start the worker from a regular terminal, outside the AI client:

```bat
source-ledger.cmd assistant start --workspace-root .sourceledger
source-ledger.cmd assistant status --workspace-root .sourceledger
source-ledger.cmd assistant jobs --workspace-root .sourceledger
```

MCP jobs only queue bounded work; they do not auto-start a worker (the legacy `queue_*` tools are hidden by default; see the legacy tools paragraph below). `start_research_plan` starts the worker for the confirmed plan. Other jobs can
accept a job while the worker is stopped, leaving that job `queued` until you
start the worker. Jobs are stored in the workspace and run one at a time. Use
the returned job ID to inspect or explicitly requeue an interrupted job:

```bat
source-ledger.cmd assistant job --workspace-root .sourceledger --job-id <JOB_ID>
source-ledger.cmd assistant resume --workspace-root .sourceledger --job-id <JOB_ID>
source-ledger.cmd assistant stop --workspace-root .sourceledger
```

`stop` records a stop request and returns immediately. The worker finishes its
current job, then exits; check `assistant status` to confirm it has stopped. A
dead worker marks its running job `interrupted`; it is never silently rerun.

## What the local MCP server can do

The assistant MCP server exposes 36 tools by default. Ten legacy `research.json` tools are hidden unless the server is started with `--legacy-tools`, which brings the total to 46 (see the legacy tools paragraph below). The primary research-plan path uses these eight: `list_research_plans`, `get_research_plan`, `create_research_plan`, `submit_research_preview`, `update_research_plan`, `confirm_research_plan`, `start_research_plan`, and `generate_research_preview`. Create a plan with the complete short or multiline request. For a preview, use the connected AI app's own search/browser tools to inspect actual references, then submit URLs and reasons through `submit_research_preview`; the host app controls its model. Alternatively, explicitly opt in to `generate_research_preview` with a local Codex CLI or Claude Code CLI provider/model. Both routes produce bounded, unverified references and never price observations. The user can add, remove, or select candidates and must confirm the displayed revision, including industry, product, and market, before `start_research_plan` collects selected pages. Exact product identifiers are optional advanced inputs for this flow.

### Compact responses

Responses are compact by default so that hosts which divert large tool results
to files (Claude Code does this for very large results) still see status:

- Plan tools (`create_research_plan`, `get_research_plan`, `update_research_plan`,
  `submit_research_preview`, `confirm_research_plan`) return a plan summary: id,
  revision, state, topic, request, conditions, candidate counts, host counts and
  `url_fingerprint` (an order-sensitive SHA-256 of the selected URLs, the same
  value the batch run pins). Pass `include_candidates=true` with `offset`/`limit`
  (at most 200) to page candidate records. `list_research_plans` and
  `get_workspace_status` list plan summaries only.
- `get_research_batch_progress` returns counts, per-status URL counts, elapsed
  time, change counts and final paths. `include_changes=true` with
  `offset`/`limit` (at most 50) adds compact change rows; `get_research_gaps`
  pages per-URL status and `get_collection_changes` pages full change rows.
- `get_research_batch_reports` returns the final XLSX/dashboard paths and a page
  of per-batch report file names (default 10, at most 50).
- `get_job_status` and `list_recent_jobs` omit raw arguments and results unless
  `include_arguments`/`include_result` is true.
- `get_job_observations` reports coverage counts; `include_coverage=true` with
  `coverage_offset`/`coverage_limit` pages coverage and section gaps.
- `wait_for_job` accepts `timeout_seconds` up to 120 (default 20) and returns
  compact status only.

For a synthetic batch job of several hundred URLs, the default batch progress, batch reports and
plan responses each stay below 3,000 characters (tests enforce this).

### URL lists and batch follow-up

`import_plan_urls(plan_id, expected_revision, path, replace=false)` reads a
`.txt` (one URL per line, `#` comments), `.csv`
(a `url` column) or `.json` (a list, or `{"urls": [...]}`) file and adds the
URLs as selected candidates in file order. The file must be inside the assistant
workspace: pass a workspace-relative path, or an absolute path under the workspace
root. Files elsewhere are rejected, so the operator copies the URL list into the
workspace first (the `collect-plan` CLI still accepts any operator path through
`--urls-file`). It returns the count, `import.file_url_fingerprint` (the
fingerprint of the file's URLs), duplicates, invalid lines and the first/last
three URLs. The plan summary reports `plan.url_fingerprint` (the fingerprint of
the plan's selected URLs); after `replace=true` the two match. Invalid
lines are reported, never corrected. Hosts should never retype URL lists into
`update_research_plan`. Like any edit, the import creates a new unconfirmed
revision.

`get_research_gaps`, `get_job_observations` and `retry_research_pages` work for
batch jobs as well as single plan jobs. `retry_research_pages` without `urls`
retries every page the job itself reports as retryable (never pages that need
access, policy or evidence review). A batch retry is a new `research_batch`
job pinned to the same plan revision and URL list; its journal merges retried
batches into the parent's results and produces a new final workbook.

### Unattended collection without a model

For scheduled or unattended runs no assistant is needed:

```bat
source-ledger.cmd collect-plan --workspace-root .sourceledger --urls-file urls.txt ^
  --industry "..." --product "..." --market "..." --yes --json
source-ledger.cmd assistant wait --workspace-root .sourceledger --job-id <JOB_ID> --timeout 7200 --json
```

`collect-plan` creates a plan (or reuses `--plan-id`), imports the file with
replace semantics, confirms it and runs one batch job in the same process. The
explicit `--yes` plus the URL file are the operator's approval; without `--yes`
it refuses before touching the workspace, and a file with invalid lines is
rejected. When `--plan-id` already has a confirmed revision with the same topic
and URL list, that revision is reused. Options: `--batch-size` (default 20),
`--concurrency` (2), `--browser-concurrency` (1), `--browser-reuse` (on; `--no-browser-reuse` disables),
`--max-seconds` per batch (600) and `--timeout` for waiting on another worker
(7200). It prints one JSON summary (status, coverage and observation counts,
final XLSX/dashboard paths, change counts, errors). Exit codes: 0 succeeded,
1 failed or interrupted, 2 refused or invalid input, 3 still running at the
wait timeout. `assistant wait` prints the same summary for an existing job.
A succeeded job describes execution only; it is not verified price evidence.

The confirmed plan uses page and time limits, records visited and unprocessed coverage, and retains source evidence in SQLite with an XLSX report. Some unrelated navigation links on a selected host may be visited within the page budget. Generic JSON-LD Product/Offer data can preserve raw prices and attributes across product categories, but embedded prices lack visual proof and remain review-only. Supported structured conditions receive deterministic evidence checks; unknown and excluded products are separately accounted for. Known-adapter prices keep scope review flags because these checks do not establish every natural-language or commercial condition. Arbitrary page layouts and unrestricted semantic checking remain unsupported; missing values are never inferred. Preview content is untrusted data, never an instruction to widen scope or collect a price.

For the UI source-targeting flow, enter a company/site URL to register it
directly. Entering only a company name, or a source keyword, instead saves a
recommendation request. Copy the request text from **Sources** into your
connected assistant; saving it does not wake or start that client. The assistant
can call `list_source_recommendation_requests`, optionally
`request_source_recommendations(query, kind)`, search with its own available
tools, and call `submit_source_recommendations(request_id, candidates, note)`.
Each candidate supplies `name`, `url`, `reason`, and `evidence_url`; when search
is unavailable or returns nothing, submit an empty candidate list and a note.
The operator reviews the candidate reasons and references in the UI and checks
which candidates to add as registered sources. The MCP server has no tool to
select recommendations on the operator's behalf.

Recommendation requests and submissions need no collection worker. Submitted
candidates remain separate from registered sources and verified observations;
assistant-provided reasons and evidence URLs are unverified. Selecting a source
does not collect it. To research, select registered sources in the UI and start
the worker for the queued guided run (up to 50 explicit sources, 120 seconds).
The older guided-research workspace begins with one product lead. Research plans
can describe a range of products, but collection remains page-bounded and does
not claim whole-catalog coverage.

Ten legacy `research.json` tools are hidden by default: `initialize_workspace`,
`set_product_identity`, `add_source_candidate`, `queue_source_discovery`,
`queue_source_proposal`, `queue_research_agent`, `queue_supported_sites`,
`queue_verification`, `queue_collection`, and `queue_report_export`. The `queue_*`
tools queue network collection without a confirmed research plan, so the default
server does not expose them. Start the server with `serve-assistant
--legacy-tools` to expose them (46 tools in total instead of 36). When enabled,
the legacy connector tools provide workspace-scoped onboarding and source management, queue
`discover`, `propose`, `agent`, `collect_sites`, `verify`, `run`, and `export`,
and return job status, observations, report metadata, and a registered local
XLSX resource for a completed report. Job inputs are bounded and remain inside
the chosen workspace. Arbitrary commands and paths outside the workspace are
rejected. Legacy `queue_*` tools do not configure providers or call a model.
`generate_research_preview` is the separate, explicit MCP path for a local
Codex CLI or Claude Code CLI model preview; the browser also has an opt-in CLI
recommendation action. A
source candidate or proposal is never treated as a verified price observation.

The generated server command is equivalent to:

```text
<repository-venv-python> -m su_crawler serve-assistant --workspace-root <absolute-workspace-path>
```

It uses stdio only. Do not run `serve-assistant` manually in the same terminal
as an MCP client; the client launches it. The older `serve-mcp --config ...`
interface remains available for an already fixed collection configuration and
is documented separately in the README.

## Verification scope and limits

Offline tests cover generated JSON/TOML, safe merges and backups, worker
lifecycle, queued jobs, workspace path boundaries, and the MCP service
contract. They do not require paid services or external transfers. A local
check also confirmed that the installed Codex CLI parses an isolated generated
configuration with a Korean/space workspace path, and that the installed
Claude Code CLI parses its project setting (its normal client approval remains
pending). Claude Desktop settings were JSON-round-tripped only. This does not
demonstrate a live Codex, Claude Code, or Claude Desktop GUI connection; use
the clients' official local MCP instructions when their settings UI differs: [Codex](https://developers.openai.com/codex/mcp),
[Claude Code](https://code.claude.com/docs/en/mcp), and
[MCP local-server guidance](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Web UI CLI recommendations

The browser UI also supports an explicit **Run in web UI** action for a saved source recommendation request. In **Connections**, select the installed Codex CLI or Claude Code CLI, choose its provider-default model or enter a model ID, set a timeout from 30 to 600 seconds (default 180), and save. The CLI must be installed and logged in on the computer running SourceLedger. See the providers’ [Codex CLI](https://developers.openai.com/codex/cli/) and [Claude Code setup](https://code.claude.com/docs/en/setup) instructions. Availability reports installation only; authentication is checked when the job runs. Native CLI authentication and account usage rules apply. SourceLedger does not store API keys, install or log in to the CLI, or silently fall back to another provider. This setting does not change the model in an open desktop conversation.

Running a request queues a local worker job. It only returns unverified recommendation candidates for manual review and selection in Sources; it does not start a price collection job or create price observations. The provider CLI may access web pages while searching. Existing collection model calls remain opt-in and default to zero.

The connection is not a cloud deployment. Codex/Claude web or mobile access,
remote artifact download, direct reuse of a host browser session and
unattended repair are outside this implementation. SourceLedger installs no
scheduler; an operator can run `collect-plan` from their own scheduled task.

The repository's `CLAUDE.md` and `.claude/` folder hold an optional Claude Code
kit (a collection skill, a collector subagent and read-only permission
defaults). It contains no credentials or machine paths.

See [MCP evidence and recovery](MCP_FOLLOWUP.md) for coverage gaps, browser evidence submission, bounded retries and checkpoint resume.
