from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .pipeline import execute
from .storage import Store


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="근거를 보존하는 가격 수집기")
    sub = parser.add_subparsers(dest="command", required=True)
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
    mcp = sub.add_parser("serve-mcp")
    mcp.add_argument("--config", required=True)
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
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
                raise ValueError("없는 출처 ID")
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
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
