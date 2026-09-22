# Local assistant connections

[English](ASSISTANT_CONNECTIONS.md) | [한국어](ASSISTANT_CONNECTIONS.ko.md)

In 0.5, the [browser UI](WEB_UI.md) can generate these settings from **Connections** and start the worker. The browser and assistant share one workspace root; the stdio connector itself remains independent of the web server. Keep using the CLI below when you want to install a generated setting directly.

SourceLedger 0.4 can expose one workspace to Codex, Claude Code, or Claude
Desktop through a local stdio MCP server. The connector is local: it starts no
web server, does not upload the workspace, and does not enable paid search or
model providers. The collection worker is a separate local process, so an MCP
client closing does not terminate a running job.

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

`--config-file PATH` overrides a client configuration target and requires
`--install`. This is required for Claude Desktop on operating systems other
than Windows and macOS.

An install merges only a missing `sourceledger` server entry. It preserves
unrelated client settings, creates a byte-for-byte backup beside the target
(`.sourceledger-<id>.bak`), and refuses to replace a same-named server with
different settings. Rename the server or inspect and merge the generated
snippet yourself in that case. The project `.mcp.json` and connection backups
are ignored by Git.

## Worker and job lifecycle

Start the worker from a regular terminal, outside the AI client:

```bat
source-ledger.cmd assistant start --workspace-root .sourceledger
source-ledger.cmd assistant status --workspace-root .sourceledger
source-ledger.cmd assistant jobs --workspace-root .sourceledger
```

The MCP server only queues bounded work; it never auto-starts a worker. It can
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

The server provides workspace-scoped onboarding and source management, queues
`discover`, `propose`, `agent`, `collect_sites`, `verify`, `run`, and `export`,
and returns job status, observations, report metadata, and a registered local
XLSX resource for a completed report. Job inputs are bounded and remain inside
the chosen workspace. Arbitrary commands, paths outside the workspace,
provider configuration, and model calls are rejected by this connector. A
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

The connection is not a cloud deployment. Codex/Claude web or mobile access,
remote artifact download, direct reuse of a host browser session, scheduling,
and unattended repair are outside this implementation.
