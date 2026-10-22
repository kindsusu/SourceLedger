import asyncio
import pytest

from su_crawler import browser_recovery as recovery
from su_crawler.collection_issues import sync_coverage_issues
from su_crawler.storage import Store


@pytest.fixture
def issue(tmp_path, monkeypatch):
    store = Store(tmp_path)
    with store.db:
        store.db.execute("INSERT INTO runs(id,config_hash,status,started_at,demo) VALUES('r','c','partial','2026-10-14',0)")
    row = sync_coverage_issues(store, 'r', [{'url': 'https://example.test/item',
        'status': 'blocked', 'reason': 'CAPTCHA interactive challenge'}])[0]
    store.close()
    monkeypatch.setattr(recovery, '_context', lambda root, job: (tmp_path, {'r'} if job == 'job' else {'other'}))
    return row


def test_attempts_durable_idempotent_and_bounded(tmp_path, issue):
    args = (tmp_path, 'job', issue['id'])
    assert recovery.prepare_browser_recovery(*args)['attempts_used'] == 0
    with pytest.raises(ValueError, match='Host browser'):
        recovery.begin_browser_recovery(*args)
    kwargs = dict(browser_available=True, challenge_interaction_supported=True)
    for index in range(2):
        started = recovery.begin_browser_recovery(*args, **kwargs)
        assert started['attempts_used'] == index + 1
        assert recovery.begin_browser_recovery(*args, **kwargs)['attempt_id'] == started['attempt_id']
        recovery.finish_browser_recovery(*args, started['attempt_id'], outcome='blocked')
    assert recovery.prepare_browser_recovery(*args)['status'] == 'needs_user'
    with pytest.raises(ValueError, match='No further'):
        recovery.begin_browser_recovery(*args, **kwargs)


def test_access_report_requires_evidence_and_does_not_resolve_or_create_prices(tmp_path, issue):
    args = (tmp_path, 'job', issue['id'])
    attempt = recovery.begin_browser_recovery(*args, browser_available=True, challenge_interaction_supported=True)['attempt_id']
    with pytest.raises(ValueError, match='exact approved'):
        recovery.finish_browser_recovery(*args, attempt, outcome='accessible', final_url='https://other.test', evidence_text='Price')
    result = recovery.finish_browser_recovery(*args, attempt, outcome='accessible',
        final_url=issue['url'], evidence_text='Product X: USD 10')
    assert result['status'] == 'access_reported' and result['evidence_verified'] is False
    assert result['session_transfer_supported'] is False
    assert recovery.finish_browser_recovery(*args, attempt, outcome='accessible',
        final_url=issue['url'], evidence_text='Product X: USD 10') == result
    with pytest.raises(ValueError, match='immutable'):
        recovery.finish_browser_recovery(*args, attempt, outcome='blocked')
    store = Store(tmp_path)
    try:
        assert not store.observations('r')
        assert store._collection_issue(issue['id'])['status'] == 'open'
    finally:
        store.close()


@pytest.mark.parametrize('final', ['https://user:pw@example.test/item', 'ftp://example.test/item', 'https://example.test:8443@evil.test/item'])
def test_access_report_rejects_credentialed_or_non_http_final_url(tmp_path, issue, final):
    args = (tmp_path, 'job', issue['id'])
    attempt = recovery.begin_browser_recovery(*args, browser_available=True, challenge_interaction_supported=True)['attempt_id']
    with pytest.raises(ValueError):
        recovery.finish_browser_recovery(*args, attempt, outcome='accessible', final_url=final, evidence_text='Product X: USD 10')
    assert recovery.prepare_browser_recovery(*args)['status'] != 'access_reported'


def test_wrong_job_and_nonchallenge_rejected(tmp_path, issue):
    with pytest.raises(ValueError, match='belonging'):
        recovery.prepare_browser_recovery(tmp_path, 'other', issue['id'])
    store = Store(tmp_path)
    with store.db:
        store.db.execute("UPDATE collection_issues SET reason_code='policy_denied' WHERE id=?", (issue['id'],))
    store.close()
    with pytest.raises(ValueError, match='browser-handoff'):
        recovery.prepare_browser_recovery(tmp_path, 'job', issue['id'])


def test_challenge_not_retried_as_rate_limit():
    from su_crawler.catalog_scheduler import _transient
    from su_crawler.models import FetchResult
    from su_crawler.collection_issues import _classify
    result = FetchResult('s', 'blocked', 'http', message='HTTP 429 access challenge',
                         trace=[{'event': 'access_challenge'}])
    assert not _transient(result)
    assert _classify('blocked', 'access_challenge')[0] == 'browser_handoff'


def test_temporary_dns_failure_is_transient_but_other_policy_denials_are_not():
    from su_crawler.catalog_scheduler import _transient
    from su_crawler.models import FetchResult

    def denied(code):
        return FetchResult('s', 'policy_denied', 'http', message=f'Request denied by policy ({code})',
                           trace=[{'event': 'policy_denied', 'code': code}])

    assert _transient(denied('dns_temporary_failure'))
    assert not _transient(denied('dns_resolution_failed'))  # the name does not exist
    assert not _transient(denied('robots_disallowed'))


def test_resolver_try_again_is_classified_as_temporary(monkeypatch):
    import socket
    from su_crawler import collectors

    def fail(errno):
        def getaddrinfo(*args, **kwargs):
            raise socket.gaierror(errno, 'resolver')
        return getaddrinfo

    monkeypatch.setattr(collectors.socket, 'getaddrinfo', fail(11002))
    with pytest.raises(collectors.PolicyError) as temporary:
        collectors._resolved_addresses('example.com', 443)
    assert temporary.value.code == 'dns_temporary_failure'
    monkeypatch.setattr(collectors.socket, 'getaddrinfo', fail(11001))
    with pytest.raises(collectors.PolicyError) as missing:
        collectors._resolved_addresses('example.invalid', 443)
    assert missing.value.code == 'dns_resolution_failed'


def test_catalog_challenge_stops_backend_fallback(tmp_path):
    from su_crawler.catalog_collection import collect_plan
    from su_crawler.models import FetchResult
    calls = []
    def fetch(source, base, backend):
        calls.append(backend)
        return FetchResult(source.id, 'blocked', backend, message='access challenge',
                           trace=[{'event': 'access_challenge'}])
    plan = {'id': 'captcha', 'revision': 1, 'state': 'confirmed',
            'topic': {'industry': 'Parts', 'product': 'Bolt', 'market': 'US'},
            'candidates': [{'url': 'https://example.test/product', 'selected': True}]}
    result = collect_plan(tmp_path, plan, output_dir=tmp_path/'out', max_pages=1,
                          collector=fetch, follow_links=False)
    assert calls == ['http']
    assert result['coverage'][0]['status'] == 'blocked'


def test_mcp_recovery_roundtrip(tmp_path, issue):
    pytest.importorskip('mcp')
    from su_crawler.assistant_server import build_assistant_server
    server = build_assistant_server(tmp_path)
    def call(name, **kwargs):
        _, result = asyncio.run(server.call_tool(name, {'job_id': 'job', 'issue_id': issue['id'], **kwargs}))
        return result
    assert call('prepare_browser_recovery')['mode'] == 'connected_host_browser'
    attempt = call('begin_browser_recovery', browser_available=True, challenge_interaction_supported=True)
    assert call('finish_browser_recovery', attempt_id=attempt['attempt_id'], outcome='needs_user')['status'] == 'needs_user'


def test_web_recovery_route_requires_csrf_and_returns_instructions(tmp_path, issue):
    from test_web_server import running_server, request, authorized, json_response
    with running_server(tmp_path) as server:
        path = '/api/jobs/job/browser-recovery'
        assert request(server, 'POST', path, {'issue_id': issue['id']})[0] == 403
        status, plan = json_response(authorized(server, 'POST', path, {'issue_id': issue['id']}))
        assert status == 200 and plan['url'] == issue['url']
        assert plan['automatic_solver_available'] is False and plan['attempts_used'] == 0


def test_browser_challenge_after_option_preserves_only_safe_capture(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from su_crawler.collectors import collect
    from su_crawler.models import Source
    monkeypatch.setenv('SOURCELEDGER_HEADLESS', '1')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'''<select id=size><option value=a>A</option><option value=b>B</option></select>
                <p id=price>10</p><script>document.querySelector('#size').onchange=()=>{
                if(document.querySelector('#size').value==='b')document.body.innerHTML=
                '<h1>Verify you are human</h1><div class="g-recaptcha"></div>';
                }</script>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = Source('s', 's', 'web', f'http://127.0.0.1:{server.server_port}/', [],
                        allowed_domains=['127.0.0.1'], internal=True, respect_robots=False,
                        timeout_seconds=10, recipe=[{'action': 'enumerate_options', 'selectors': ['#size'],
                                                    'progress_wait_seconds': 0.01}])
        result = collect(source, str(tmp_path), 'playwright')
        assert result.status == 'blocked', result.message
        assert not result.content
        assert len(result.variant_snapshots) == 1
        assert result.variant_snapshots[0]['selection_values'] == {'#size': 'a'}
        assert any(event['event'] == 'access_challenge' for event in result.trace)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
