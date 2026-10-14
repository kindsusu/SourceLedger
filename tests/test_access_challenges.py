import pytest
import httpx

from su_crawler.access_challenges import detect_challenge
from su_crawler import collectors
from su_crawler.collectors import collect
from su_crawler.models import Source


@pytest.mark.parametrize("markup, kind", [
    ('<h1>Verify you are human</h1><div class="g-recaptcha" data-sitekey="site"></div>', "captcha"),
    ('<h1>Complete the CAPTCHA</h1><iframe src="https://newassets.hcaptcha.com/captcha/v1"></iframe>', "captcha"),
    ('<h1>Security check</h1><div class="cf-turnstile" data-sitekey="site"></div>', "captcha"),
    ('<h1>로봇이 아닙니다</h1><div class="g-recaptcha" data-sitekey="site"></div>', "captcha"),
    ('<h1>사람인지 확인</h1><iframe src="https://newassets.hcaptcha.com/captcha/v1"></iframe>', "captcha"),
    ('<h1>자동입력 방지</h1><div class="cf-turnstile" data-sitekey="site"></div>', "captcha"),
    ('<h1>人間であることを確認</h1><div class="g-recaptcha" data-sitekey="site"></div>', "captcha"),
    ('<h1>ロボットではありません</h1><iframe src="https://newassets.hcaptcha.com/captcha/v1"></iframe>', "captcha"),
    ('<form id="challenge-form" action="/cdn-cgi/challenge-platform/h/g/flow"></form>', "cloudflare"),
    ('<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/h/g/orchestrate"></script>', "cloudflare"),
])
def test_detect_active_challenge(markup, kind):
    assert detect_challenge(markup)["type"] == kind


@pytest.mark.parametrize("markup", [
    '<article>Our CAPTCHA solving guide costs $30.</article>',
    '<a href="/captcha-help">Learn about CAPTCHA</a><p>Price 100</p>',
    '<script src="https://www.google.com/recaptcha/api.js"></script><p>Price 100</p>',
    '<title>Just a moment...</title><p>Price 100</p>',
    '<h1>Product details</h1><p>Price: $100</p><form id="contact"><div class="g-recaptcha"></div></form>',
    '<h1>Security check kit</h1><p>Add to cart</p><div class="cf-turnstile"></div>',
    '<p>Price 100</p><iframe hidden src="https://www.google.com/recaptcha/api2/anchor"></iframe>',
    '<h1>로봇이 아닙니다 안내서</h1><p>가격: 10000원</p><form id="contact"><div class="g-recaptcha"></div></form>',
    '<h1>ロボットではありません ガイド</h1><p>価格：1000円</p><form id="contact"><div class="h-captcha"></div></form>',
])
def test_prose_or_idle_script_is_not_challenge(markup):
    assert detect_challenge(markup) is None


def _source(path):
    return Source('s', 's', 'web', 'https://example.com/' + path, [],
                  allowed_domains=['example.com'], respect_robots=False)


def _mock_response(monkeypatch, html, status=200):
    monkeypatch.setattr(collectors, '_check_url', lambda *_: None)
    def fetch(_client, _source, url, **_kwargs):
        return httpx.Response(status, headers={'content-type': 'text/html'}, content=html.encode(),
                              request=httpx.Request('GET', url)), []
    monkeypatch.setattr(collectors, '_get_following_policy', fetch)


def test_http_collector_blocks_challenge_without_content(tmp_path, monkeypatch):
    _mock_response(monkeypatch, '<h1>Verify you are human</h1><div class="g-recaptcha" data-sitekey="site"></div>')
    result = collect(_source('challenge'), str(tmp_path), 'http')
    assert result.status == 'blocked', result.message
    assert not result.content
    assert any(item['event'] == 'access_challenge' and item['type'] == 'captcha' for item in result.trace)


def test_http_product_description_is_fetched(tmp_path, monkeypatch):
    _mock_response(monkeypatch, '<article>CAPTCHA design book, price 100</article>')
    result = collect(_source('product'), str(tmp_path), 'http')
    assert result.status == 'fetched'
    assert b'price 100' in result.content
