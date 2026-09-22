"""Loopback-only HTTP interface for the SourceLedger local web application."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
import importlib.util
import json
import math
import mimetypes
import secrets
import webbrowser

from . import __version__
from .assistant_runtime import (
    get_job, list_jobs, resume_job, runtime_status, start_worker, stop_worker, submit_job,
)
from .assistant_workspace import (
    prepare_job_args, research_path, result_observations, result_report, workspace_guard, workspace_root,
)
from .connections import CLIENTS, connect
from .research import _canonical_url, add_source, init_workspace, load_workspace, research_status, set_product


MAX_BODY_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 15
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/mark.svg": "mark.svg",
}


class WebHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying immutable workspace and browser security state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, root: Path, port: int):
        self.workspace = root
        self.csrf_token = secrets.token_urlsafe(32)
        self.static_root = Path(__file__).with_name("web")
        super().__init__(("127.0.0.1", port), SourceLedgerHandler)
        self.url = f"http://127.0.0.1:{self.server_port}"


class SourceLedgerHandler(BaseHTTPRequestHandler):
    server: WebHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: Any) -> None:
        # A local UI should not emit URLs or workspace details to stderr by default.
        return

    def _security_headers(self) -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }

    def _send(self, status: int, body: bytes = b"", *, content_type: str = "application/json; charset=utf-8",
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in self._security_headers().items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _post_error(self, status: int, message: str) -> None:
        self._discard_bounded_body()
        self.close_connection = True
        body = json.dumps({"error": message}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, extra_headers={"Connection": "close"})

    def _validated_host(self) -> str:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            raise PermissionError("Exactly one Host header is required")
        host = hosts[0]
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            raise PermissionError("Invalid Host header")
        return host

    def _validate_request(self, *, post: bool = False) -> None:
        host = self._validated_host()
        if self.headers.get_all("Transfer-Encoding", []):
            raise ValueError("Transfer-Encoding is not supported")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise PermissionError("Cross-site requests are not allowed")
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            raise PermissionError("Cross-origin requests are not allowed")
        if post:
            if origin != f"http://{host}":
                raise PermissionError("A same-origin Origin header is required")
            if not secrets.compare_digest(self.headers.get("X-SourceLedger-Token", ""), self.server.csrf_token):
                raise PermissionError("Invalid CSRF token")

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise ValueError("Exactly one Content-Length header is required")
        raw_length = lengths[0]
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError(f"Request body must not exceed {MAX_BODY_BYTES // 1024} KiB")
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError) as exc:
            raise ValueError("Request body timed out") from exc
        self._body_bytes_read += len(raw)
        if len(raw) != length:
            raise ValueError("Incomplete request body")

        def reject_constant(value: str) -> None:
            raise ValueError(f"Invalid JSON number: {value}")

        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("Request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        try:
            self._require_finite_json(value)
        except RecursionError as exc:
            raise ValueError("Request JSON is nested too deeply") from exc
        return value

    def _discard_bounded_body(self) -> None:
        """Drain an ordinary bounded body so Windows can deliver the error before close."""
        if self.headers.get_all("Transfer-Encoding", []):
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            return
        try:
            length = int(lengths[0])
        except ValueError:
            return
        if not 0 <= length <= MAX_BODY_BYTES:
            return
        try:
            remaining = max(0, length - getattr(self, "_body_bytes_read", 0))
            while remaining:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    break
                self._body_bytes_read += len(chunk)
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass

    @classmethod
    def _require_finite_json(cls, value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        if isinstance(value, dict):
            for item in value.values():
                cls._require_finite_json(item)
        elif isinstance(value, list):
            for item in value:
                cls._require_finite_json(item)

    @staticmethod
    def _fields(value: dict[str, Any], *, allowed: set[str], required: set[str] = frozenset()) -> None:
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unknown fields: {sorted(unknown)}")
        missing = required - set(value)
        if missing:
            raise ValueError(f"Missing fields: {sorted(missing)}")

    def _bootstrap(self) -> dict[str, Any]:
        path = research_path(self.server.workspace)
        configured = path.is_file()
        workspace = load_workspace(path) if configured else None
        return {
            "csrf_token": self.server.csrf_token,
            "version": __version__,
            "workspace_root": str(self.server.workspace),
            "status": "configured" if configured else "needs_setup",
            "workspace": workspace,
            "research": research_status(path) if configured else None,
            "worker": runtime_status(self.server.workspace),
            "jobs": list_jobs(self.server.workspace, limit=20),
            "mcp_available": importlib.util.find_spec("mcp") is not None,
        }

    def do_GET(self) -> None:
        try:
            self._validate_request()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if path in {"/api/bootstrap", "/api/state"}:
                if query:
                    raise ValueError("This endpoint does not accept query parameters")
                self._json(200, self._bootstrap())
                return
            if path.startswith("/api/jobs/"):
                self._get_job_route(path, query)
                return
            if path.startswith("/api/"):
                self._error(404, "API endpoint not found")
                return
            self._serve_static(path, query)
        except PermissionError as exc:
            self._error(403, str(exc))
        except FileNotFoundError as exc:
            self._error(404, str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, str(exc))
        except (FileExistsError, RuntimeError) as exc:
            self._error(409, str(exc))
        except Exception:
            self._error(500, "Internal server error")

    def _get_job_route(self, path: str, query: dict[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) not in {3, 4} or parts[:2] != ["api", "jobs"] or not parts[2]:
            self._error(404, "API endpoint not found")
            return
        job = get_job(self.server.workspace, parts[2])
        if len(parts) == 3:
            if query:
                raise ValueError("This endpoint does not accept query parameters")
            self._json(200, job)
        elif parts[3] == "observations":
            unknown = set(query) - {"offset", "limit"}
            if unknown or any(len(values) != 1 for values in query.values()):
                raise ValueError("Invalid observation query parameters")
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
            except ValueError as exc:
                raise ValueError("offset and limit must be integers") from exc
            self._json(200, result_observations(self.server.workspace, job, offset=offset, limit=limit))
        elif parts[3] == "report":
            if query:
                raise ValueError("This endpoint does not accept query parameters")
            metadata = result_report(self.server.workspace, job)
            report = Path(metadata["path"])
            self._send(200, report.read_bytes(), content_type=metadata["media_type"], extra_headers={
                "Content-Disposition": f'attachment; filename="{parts[2]}.xlsx"',
            })
        else:
            self._error(404, "API endpoint not found")

    def _serve_static(self, path: str, query: dict[str, list[str]]) -> None:
        if query or path not in STATIC_FILES:
            self._error(404, "File not found")
            return
        if self.server.static_root.is_symlink():
            self._error(404, "File not found")
            return
        root = self.server.static_root.resolve()
        candidate = self.server.static_root / STATIC_FILES[path]
        if candidate.is_symlink() or not candidate.is_file() or candidate.resolve().parent != root:
            self._error(404, "File not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".js":
            content_type = "text/javascript"
        self._send(200, candidate.read_bytes(), content_type=f"{content_type}; charset=utf-8")

    def do_POST(self) -> None:
        self._body_bytes_read = 0
        try:
            self._validate_request(post=True)
            parsed = urlsplit(self.path)
            if parsed.query:
                raise ValueError("POST endpoints do not accept query parameters")
            path = unquote(parsed.path)
            value = self._read_json()
            result = self._post_route(path, value)
            self._json(200, result)
        except PermissionError as exc:
            self._post_error(403, str(exc))
        except FileNotFoundError as exc:
            self._post_error(404, str(exc))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._post_error(400, str(exc))
        except (FileExistsError, RuntimeError) as exc:
            self._post_error(409, str(exc))
        except Exception:
            self._post_error(500, "Internal server error")

    def _post_route(self, path: str, value: dict[str, Any]) -> dict[str, Any]:
        base = self.server.workspace
        if path == "/api/workspace":
            self._fields(value, allowed={"industry", "product", "market", "locale"},
                         required={"industry", "product", "market"})
            with workspace_guard(base):
                workspace = init_workspace(research_path(base), industry=value["industry"], product=value["product"],
                                           market=value["market"], locale=value.get("locale", "en"))
            return {"workspace": workspace, "status": "configured"}
        if path == "/api/product":
            self._fields(value, allowed={"identifiers", "required_specs"}, required={"identifiers"})
            with workspace_guard(base):
                workspace = set_product(research_path(base), identifiers=value["identifiers"],
                                        required_specs=value.get("required_specs"))
            return {"product": workspace["product"]}
        if path == "/api/sources":
            self._fields(value, allowed={"url", "scope", "name"}, required={"url"})
            canonical, _ = _canonical_url(value["url"])
            with workspace_guard(base):
                workspace = add_source(research_path(base), url=value["url"], scope=value.get("scope", "public"),
                                       name=value.get("name"))
            source = next(item for item in workspace["sources"] if item["location"] == canonical)
            return {"source": source, "status": "registered", "candidate_source_count": len(workspace["sources"])}
        if path == "/api/worker":
            self._fields(value, allowed={"action"}, required={"action"})
            if value["action"] == "start":
                return start_worker(base)
            if value["action"] == "stop":
                return stop_worker(base)
            raise ValueError("action must be start or stop")
        if path == "/api/jobs":
            self._fields(value, allowed={"operation", "arguments"}, required={"operation", "arguments"})
            if not isinstance(value["operation"], str):
                raise ValueError("operation must be a string")
            if not isinstance(value["arguments"], dict):
                raise ValueError("arguments must be an object")
            with workspace_guard(base):
                prepared = prepare_job_args(base, value["operation"], value["arguments"])
            job = submit_job(base, value["operation"], prepared)
            return {**job, "worker": runtime_status(base)}
        if path.startswith("/api/jobs/") and path.endswith("/resume"):
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "jobs"] or parts[3] != "resume":
                raise FileNotFoundError("API endpoint not found")
            self._fields(value, allowed=set())
            return resume_job(base, parts[2])
        if path == "/api/connections":
            self._fields(value, allowed={"client"}, required={"client"})
            if value["client"] not in CLIENTS:
                raise ValueError("client must be codex, claude-code, or claude-desktop")
            output = base / "connections"
            if output.is_symlink() or (output.exists() and output.resolve().parent != base):
                raise ValueError("Connection directory must stay inside the workspace")
            with workspace_guard(base):
                result = connect(value["client"], workspace_root=base, output_dir=output, install=False)
            snippet_path = Path(result["snippet_path"])
            if snippet_path.is_symlink() or snippet_path.resolve().parent != output.resolve():
                raise ValueError("Generated connection snippet is outside the workspace")
            snippet = snippet_path.read_text(encoding="utf-8")
            return {**result, "snippet": snippet}
        raise FileNotFoundError("API endpoint not found")

    def do_OPTIONS(self) -> None:
        self._error(405, "Method not allowed")

    def do_PUT(self) -> None:
        self._error(405, "Method not allowed")

    def do_DELETE(self) -> None:
        self._error(405, "Method not allowed")


def build_web_server(root: str | Path, *, port: int = 8765) -> WebHTTPServer:
    """Build an IPv4-loopback HTTP server scoped to one workspace."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    try:
        return WebHTTPServer(workspace_root(root), port)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {48, 98}:
            raise OSError(f"Port {port} is already in use; choose another port with --port") from exc
        raise


def serve_web(root: str | Path, *, port: int = 8765, open_browser: bool = True) -> None:
    """Serve the local web application until interrupted."""
    server = build_web_server(root, port=port)
    try:
        print(f"SourceLedger web: {server.url}", flush=True)
        print(f"Workspace: {server.workspace}", flush=True)
        print("Press Ctrl+C to stop the web server.", flush=True)
        if open_browser:
            webbrowser.open(server.url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
