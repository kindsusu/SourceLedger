from types import SimpleNamespace

import pytest

from browser_diagnostics import capture_browser_diagnostics


class FakePage:
    def __init__(self):
        self.calls = []
        self.context = SimpleNamespace(tracing=self)

    def start(self, **kwargs):
        self.calls.append(('start', kwargs))

    def stop(self, **kwargs):
        self.calls.append(('stop', kwargs))

    def screenshot(self, **kwargs):
        self.calls.append(('screenshot', kwargs))


def test_diagnostics_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv('SOURCELEDGER_TEST_ARTIFACTS', raising=False)
    page = FakePage()
    with capture_browser_diagnostics(page, SimpleNamespace()):
        pass
    assert page.calls == []


def test_failure_saves_diagnostics_but_success_discards_trace(tmp_path, monkeypatch):
    monkeypatch.setenv('SOURCELEDGER_TEST_ARTIFACTS', str(tmp_path))
    for failed in (False, True):
        page = FakePage()
        request = SimpleNamespace(node=SimpleNamespace(nodeid=f'test[{failed}]',
            report_call=SimpleNamespace(failed=failed)))
        with capture_browser_diagnostics(page, request):
            pass
        assert ('screenshot' in [name for name, _ in page.calls]) == failed
        assert ('path' in page.calls[-1][1]) == failed
    assert len(list(tmp_path.glob('*/diagnostic.json'))) == 1


def test_capture_error_does_not_hide_original_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('SOURCELEDGER_TEST_ARTIFACTS', str(tmp_path))
    page = FakePage()
    def broken(**kwargs):
        raise RuntimeError('capture error')
    page.screenshot = broken
    request = SimpleNamespace(node=SimpleNamespace(nodeid='test-failure'))
    with pytest.raises(ValueError, match='original'):
        with capture_browser_diagnostics(page, request):
            raise ValueError('original')
    assert 'RuntimeError' in next(tmp_path.glob('*/diagnostic.json')).read_text()


def test_unwritable_artifacts_do_not_hide_original_failure(tmp_path, monkeypatch):
    output = tmp_path / 'file'
    output.write_text('not a directory')
    monkeypatch.setenv('SOURCELEDGER_TEST_ARTIFACTS', str(output))
    request = SimpleNamespace(node=SimpleNamespace(nodeid='test-failure'))
    with pytest.raises(ValueError, match='original'):
        with capture_browser_diagnostics(FakePage(), request):
            raise ValueError('original')
