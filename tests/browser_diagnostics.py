"""Opt-in diagnostics for local synthetic test pages, never research profiles."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path


@contextmanager
def capture_browser_diagnostics(page, request):
    output = os.environ.get('SOURCELEDGER_TEST_ARTIFACTS')
    if not output:
        yield page
        return
    folder = Path(output) / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    tracing = page.context.tracing
    errors = []
    started = False
    body_failed = False
    try:
        tracing.start(screenshots=True, snapshots=True, sources=False)
        started = True
    except Exception as exc:
        errors.append('trace_start: ' + type(exc).__name__)
    try:
        yield page
    except BaseException:
        body_failed = True
        raise
    finally:
        failed = body_failed or any(getattr(getattr(request.node, 'report_' + phase, None), 'failed', False)
                                    for phase in ('setup', 'call'))
        if failed:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder / 'screenshot.png'), timeout=5000)
            except Exception as exc:
                errors.append('screenshot: ' + type(exc).__name__)
        if started:
            try:
                tracing.stop(**({'path': str(folder / 'trace.zip')} if failed else {}))
            except Exception as exc:
                errors.append('trace_stop: ' + type(exc).__name__)
        if failed:
            # Never dump page HTML, environment variables or host credentials.
            try:
                (folder / 'diagnostic.json').write_text(json.dumps({
                    'test': request.node.nodeid, 'capture_errors': errors,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError:
                pass  # Artifact failures must not hide the original test failure.
