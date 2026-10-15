"""Optional, explicitly selected UI dismissal never chooses an ambiguous target."""
import pytest

from su_crawler.collectors import _safe_step, collect
from su_crawler.doctor import doctor
from su_crawler.models import Source
from test_browser import server


@pytest.mark.parametrize('step', [
    {'action': 'click', 'selector': 'button', 'if_visible': 'true'},
    {'action': 'fill', 'selector': 'input', 'if_visible': True},
])
def test_optional_click_requires_explicit_boolean_and_click_action(step):
    with pytest.raises(ValueError, match='if_visible'):
        _safe_step(step)


def test_optional_click_present_absent_and_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv('SOURCELEDGER_HEADLESS', '1')
    if next(x for x in doctor() if x['backend'] == 'playwright')['status'] != 'available':
        pytest.skip('Browser runtime unavailable')
    with server() as url:
        source = Source('optional', 'optional', 'web', url, [], internal=True,
                        allowed_domains=['127.0.0.1'], respect_robots=False, timeout_seconds=10,
                        recipe=[{'action': 'click', 'selector': '#not-present', 'if_visible': True},
                                {'action': 'select', 'selector': '#size', 'value': 'large'},
                                {'action': 'click', 'selector': '#apply', 'if_visible': True}])
        result = collect(source, str(tmp_path), 'playwright')
        assert result.status == 'fetched', result.message
        assert b'<div id="price">200</div>' in result.content
        assert [e['reason'] for e in result.trace if e['event'] == 'recipe_step_skipped'] == ['not_visible']
        source.recipe = [{'action': 'click', 'selector': '#apply, #size', 'if_visible': True}]
        result = collect(source, str(tmp_path), 'playwright')
        assert result.status == 'policy_denied'
        assert any(e.get('code') == 'invalid_recipe' for e in result.trace)
