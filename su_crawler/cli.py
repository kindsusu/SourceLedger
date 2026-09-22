from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .pipeline import execute
from .storage import Store


DEFAULT_WORKSPACE = ".sourceledger/research.json"


def _pairs(values: list[str] | None) -> dict[str, str]:
    result = {}
    for value in values or []:
        key, separator, text = value.partition("=")
        key, text = key.strip(), text.strip()
        if not separator or not key or not text or key in result:
            raise ValueError("Use unique, non-empty KEY=VALUE entries.")
        result[key] = text
    return result


def _init(args) -> dict:
    from .research import init_workspace
    prompts = {
        "en": {"industry": "Industry", "product": "Product or product group", "market": "Target market or region"},
        "ko": {"industry": "산업군", "product": "상품 또는 상품군", "market": "대상 시장 또는 지역"},
    }
    values = {key: getattr(args, key) for key in prompts[args.lang]}
    missing = [key for key, value in values.items() if not value or not value.strip()]
    if missing and not sys.stdin.isatty():
        raise ValueError("Non-interactive setup requires " + ", ".join("--" + key for key in missing))
    for key in missing:
        values[key] = input(prompts[args.lang][key] + ": ").strip()
    return init_workspace(args.workspace, **values, locale=args.lang)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="SourceLedger: collect prices and preserve source evidence.")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("init", help="Create a research workspace; prompt for missing topic fields.")
    setup.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    setup.add_argument("--lang", choices=["en", "ko"], default="en")
    for field in ("industry", "product", "market"):
        setup.add_argument("--" + field)
    research = sub.add_parser("research-status", help="Show candidates, readiness, and next actions.")
    research.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    sites = sub.add_parser('collect-sites', help='Collect supplied supported rental pages directly into SQLite and XLSX.')
    sites.add_argument('--workspace', default=DEFAULT_WORKSPACE)
    sites.add_argument('--url', action='append')
    sites.add_argument('--output-dir')
    sites.add_argument('--max-pages', type=int, default=5)
    sites.add_argument('--max-seconds', type=float, default=120)
    sites.add_argument('--no-incremental', action='store_true')
    source = sub.add_parser("source-add", help="Save an authorized URL candidate without fetching it.")
    source.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    source.add_argument("--url", required=True)
    source.add_argument("--scope", choices=["public", "internal"], default="public")
    source.add_argument("--name")
    candidates = sub.add_parser("source-discover", help="Discover bounded links from one saved candidate.")
    candidates.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    candidates.add_argument("--source-id", required=True)
    candidates.add_argument("--limit", type=int, default=100)
    search = sub.add_parser("search", help="Find candidate sources through an explicitly configured SearXNG server.")
    search.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    search.add_argument("--provider-config")
    search.add_argument("--query")
    search.add_argument("--limit", type=int, default=10)
    proposal = sub.add_parser("propose", help="Propose source-backed extraction rules without activating them.")
    proposal.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    proposal.add_argument("--source-id", required=True)
    proposal.add_argument("--output-dir", required=True)
    proposal.add_argument("--model-config")
    proposal.add_argument("--max-model-calls", type=int, default=0)
    proposal.add_argument("--timeout", type=float, default=30)
    agent = sub.add_parser("agent", help="Run or resume a bounded search, proposal, and verification batch.")
    agent.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    agent.add_argument("--run-dir", required=True)
    agent.add_argument("--search-config")
    agent.add_argument("--model-config")
    agent.add_argument("--samples")
    agent.add_argument("--source-id", action="append")
    agent.add_argument("--max-sources", type=int, default=3)
    agent.add_argument("--max-model-calls", type=int, default=0)
    agent.add_argument("--max-seconds", type=float, default=120)
    agent.add_argument("--max-steps", type=int)
    agent.add_argument("--activate", action="store_true")
    agent.add_argument("--resume", action="store_true")
    product = sub.add_parser("product-set", help="Replace explicit product identifiers and optional specifications.")
    product.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    product.add_argument("--identifier", action="append", required=True, metavar="KEY=VALUE")
    product.add_argument("--spec", action="append", metavar="KEY=VALUE", help="Replace required specs when supplied.")
    draft = sub.add_parser("draft", help="Write an unvalidated collection config for saved candidates.")
    draft.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    draft.add_argument("--output", required=True)
    verify = sub.add_parser("verify", help="Collect and validate the exact configuration and optional known samples.")
    verify.add_argument("--config", required=True)
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--samples")
    activate = sub.add_parser("activate", help="Write a config only when its validation receipt still passes.")
    activate.add_argument("--config", required=True)
    activate.add_argument("--receipt", required=True)
    activate.add_argument("--output", required=True)
    for name in ("run", "status", "observations", "export"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "run":
            p.add_argument("--resume")
            p.add_argument("--max-tasks", type=int)
        else:
            p.add_argument("--run-id", required=True)
    sub.add_parser("doctor")
    discovery = sub.add_parser("discover")
    discovery.add_argument("--config", required=True)
    discovery.add_argument("--source-id", required=True)
    discovery.add_argument("--limit", type=int, default=100)
    connection = sub.add_parser("connect", help="Generate or safely install local assistant MCP settings.")
    connection.add_argument("--client", choices=["codex", "claude-code", "claude-desktop"], required=True)
    connection.add_argument("--workspace-root", default=".sourceledger")
    connection.add_argument("--output", help="Directory for generated connection snippets.")
    connection.add_argument("--name", default="sourceledger")
    connection.add_argument("--install", action="store_true", help="Merge the server into client settings; preserve a backup.")
    connection.add_argument("--config-file", help="Override client settings file (requires --install).")
    connection.add_argument("--project-dir", help="Project directory for Claude Code settings (default: current directory).")
    assistant = sub.add_parser("assistant", help="Manage the independent local assistant worker.")
    assistant.add_argument("action", choices=["start", "status", "stop", "jobs", "job", "resume"])
    assistant.add_argument("--workspace-root", default=".sourceledger")
    assistant.add_argument("--job-id", help="Job identifier for job or resume.")
    assistant.add_argument("--limit", type=int, default=20, help="Maximum jobs to list (1-100).")
    assistant_mcp = sub.add_parser("serve-assistant", help="Serve workspace onboarding and queued research tools over local stdio MCP.")
    assistant_mcp.add_argument("--workspace-root", default=".sourceledger")
    web_ui = sub.add_parser("ui", help="Open the local browser workspace and MCP connection settings.")
    web_ui.add_argument("--workspace-root", default=".sourceledger")
    web_ui.add_argument("--port", type=int, default=8765, help="Local HTTP port (0 selects an available port).")
    web_ui.add_argument("--no-browser", action="store_true", help="Print the local URL without opening a browser.")
    mcp = sub.add_parser("serve-mcp")
    mcp.add_argument("--config", required=True)
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "ui":
            from .web_server import serve_web
            serve_web(args.workspace_root, port=args.port, open_browser=not args.no_browser)
            return 0
        elif args.command == "connect":
            from .connections import connect
            result = connect(args.client, workspace_root=args.workspace_root, output_dir=args.output,
                             name=args.name, install=args.install, config_file=args.config_file,
                             project_dir=args.project_dir)
        elif args.command == "assistant":
            from .assistant_runtime import start_worker, stop_worker, runtime_status, list_jobs, get_job, resume_job
            root = Path(args.workspace_root)
            if args.action in {"job", "resume"}:
                if not args.job_id:
                    raise ValueError("--job-id is required for assistant job or resume")
                operation = get_job if args.action == "job" else resume_job
                result = operation(root, args.job_id)
            elif args.job_id:
                raise ValueError("--job-id is only available for assistant job or resume")
            elif args.action == "jobs":
                result = {"jobs": list_jobs(root, limit=args.limit)}
            else:
                operation = {"start": start_worker, "stop": stop_worker, "status": runtime_status}[args.action]
                result = operation(root)
        elif args.command == "serve-assistant":
            import importlib.util
            if importlib.util.find_spec("mcp") is None:
                raise ValueError("MCP is not installed. Run setup.cmd --mcp or bash setup.sh --mcp first")
            from .assistant_server import build_assistant_server
            server = build_assistant_server(Path(args.workspace_root).expanduser())
            server.run(transport="stdio")
            return 0
        elif args.command == "init":
            result = _init(args)
        elif args.command == 'collect-sites':
            from .site_collection import collect_sites
            result = collect_sites(args.workspace, urls=args.url, output_dir=args.output_dir,
                max_pages=args.max_pages, max_seconds=args.max_seconds, incremental=not args.no_incremental)
        elif args.command in {"research-status", "source-add", "source-discover", "product-set", "draft"}:
            from . import research
            if args.command == "research-status":
                result = research.research_status(args.workspace)
            elif args.command == "source-add":
                result = research.add_source(args.workspace, url=args.url, scope=args.scope, name=args.name)
            elif args.command == "source-discover":
                result = research.discover_candidates(args.workspace, source_id=args.source_id, limit=args.limit)
            elif args.command == "product-set":
                result = research.set_product(args.workspace, identifiers=_pairs(args.identifier),
                                              required_specs=_pairs(args.spec) if args.spec is not None else None)
            else:
                result = research.generate_draft(args.workspace, output_path=args.output)
        elif args.command == "search":
            from .search import search_workspace
            result = search_workspace(args.workspace, provider_path=args.provider_config, query=args.query, limit=args.limit)
        elif args.command == "propose":
            from .proposals import propose_source
            result = propose_source(args.workspace, source_id=args.source_id, output_dir=args.output_dir,
                                    model_config_path=args.model_config, timeout_seconds=args.timeout,
                                    max_model_calls=args.max_model_calls)
        elif args.command == "agent":
            from .agent import run_agent
            result = run_agent(args.workspace, run_dir=args.run_dir, search_config_path=args.search_config,
                               model_config_path=args.model_config, samples_path=args.samples, source_ids=args.source_id,
                               max_sources=args.max_sources, max_model_calls=args.max_model_calls,
                               max_seconds=args.max_seconds, max_steps=args.max_steps,
                               activate=args.activate, resume=args.resume)
        elif args.command == "verify":
            from .activation import verify_config
            result = verify_config(args.config, receipt_path=args.receipt, samples_path=args.samples)
        elif args.command == "activate":
            from .activation import activate_config
            result = activate_config(args.config, receipt_path=args.receipt, output_path=args.output)
        elif args.command == "doctor":
            from .doctor import doctor
            result = doctor()
        elif args.command == "serve-mcp":
            from .mcp_server import build_server
            server = build_server(Path(args.config).resolve(), port=args.port)
            server.run(transport=args.transport)
            return 0
        elif args.command == "discover":
            from .discovery import discover
            config = load_config(args.config)
            source = next((s for s in config.sources if s.id == args.source_id), None)
            if source is None:
                raise ValueError("Unknown source ID.")
            result = discover(source, config.base_dir, limit=args.limit)
        else:
            config = load_config(args.config)
            if args.command == "run":
                result = execute(config, resume_id=args.resume, max_tasks=args.max_tasks)
            else:
                store = Store(Path(config.output_dir))
                try:
                    if args.command == "status":
                        result = {"run": store.run(args.run_id), "tasks": store.tasks(args.run_id)}
                    elif args.command == "observations":
                        result = store.observations(args.run_id)
                    else:
                        from .pipeline import report_for_run
                        from .storage import workspace_lock
                        with workspace_lock(Path(config.output_dir)):
                            result = report_for_run(config, store, args.run_id)
                finally:
                    store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if args.command in {"search", "propose", "agent", "collect-sites"}:
            return 0 if result.get("status") in {"searched", "proposed", "completed", "paused"} else 1
        return 1 if args.command == "verify" and not result["eligible"] else 0
    except (ValueError, OSError, RuntimeError, EOFError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
