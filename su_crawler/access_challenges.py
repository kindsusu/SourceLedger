"""Conservative detection of active access challenges in HTML documents."""
from __future__ import annotations

from html.parser import HTMLParser
import re


_CAPTCHA_HOST = re.compile(r"(?:^|[/.])(?:google\.com/recaptcha|recaptcha\.net/recaptcha|hcaptcha\.com|challenges\.cloudflare\.com/turnstile)(?:[/?#.]|$)", re.I)
_CF_PLATFORM = re.compile(r"/cdn-cgi/challenge-platform/", re.I)
_CF_TITLE = re.compile(r"^(?:just a moment\.{0,3}|attention required!?|one more step)$", re.I)
_GATE_TEXT = re.compile(r"\b(?:verify (?:that )?you(?:'re| are) (?:a )?human|confirm you(?:'re| are) human|"
                        r"complete (?:the )?captcha|security check|checking your browser|"
                        r"unusual traffic|prove (?:that )?you(?:'re| are) (?:a )?human)\b", re.I)
_COMMERCE_TEXT = re.compile(r"\b(?:add to cart|buy now|checkout|product details|price\s*[:$€£])", re.I)
_LOCAL_GATE_TEXT = re.compile(r"로봇이\s*아닙니다|사람인지\s*확인|자동\s*입력\s*방지|"
                              r"人間であることを確認|ロボットではありません")
_LOCAL_COMMERCE_TEXT = re.compile(r"가격\s*[:：]|장바구니|구매하기|価格\s*[:：]|カートに入れる|購入する")


class _ChallengeHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.signals: set[str] = set()
        self.title: list[str] = []
        self.visible_text: list[str] = []
        self._in_title = False
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "").lower() for key, value in attrs}
        marker = " ".join(values.get(key, "") for key in ("id", "class", "name"))
        url = " ".join(values.get(key, "") for key in ("src", "data-src", "action"))
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style"}:
            self._hidden_depth += 1
        if tag == "iframe" and _CAPTCHA_HOST.search(url):
            self.signals.add("captcha_iframe")
        if tag in {"div", "form", "iframe"}:
            if re.search(r"(?:^|\s)(?:g-recaptcha|h-captcha|cf-turnstile)(?:\s|$)", marker):
                self.signals.add("captcha_widget")
            if tag == "form" and _CF_PLATFORM.search(url):
                self.signals.add("cloudflare_form")
            if "cf-chl-widget" in marker:
                self.signals.add("cloudflare_widget")
        if tag == "script" and _CF_PLATFORM.search(url):
            self.signals.add("cloudflare_script")
        if tag == "script" and _CAPTCHA_HOST.search(url):
            self.signals.add("captcha_script")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._hidden_depth:
            self.visible_text.append(data)


def detect_challenge(html: bytes | str, status: int = 200) -> dict[str, str] | None:
    """Return a stable challenge type/reason only for active challenge markup.

    Status alone and prose mentioning CAPTCHA are deliberately insufficient.
    """
    if not isinstance(html, (bytes, str)):
        return None
    sample = html[:500_000]
    if isinstance(sample, bytes):
        sample = sample.decode("utf-8", errors="replace")
    parser = _ChallengeHTML()
    parser.feed(sample)
    signals = parser.signals
    visible = " ".join(parser.visible_text)
    title = " ".join(parser.title)
    page_text = title + " " + visible
    gate_context = bool(_GATE_TEXT.search(page_text) or _LOCAL_GATE_TEXT.search(page_text)) and not (
        _COMMERCE_TEXT.search(visible) or _LOCAL_COMMERCE_TEXT.search(visible)
    )
    if "captcha_iframe" in signals and (status in {403, 429} or gate_context):
        return {"type": "captcha", "reason": "captcha_iframe"}
    if "captcha_widget" in signals and (status in {403, 429} or gate_context):
        return {"type": "captcha", "reason": "captcha_widget"}
    if "cloudflare_form" in signals:
        return {"type": "cloudflare", "reason": "cloudflare_challenge_form"}
    if "cloudflare_widget" in signals and (status in {403, 429} or gate_context):
        return {"type": "cloudflare", "reason": "cloudflare_challenge_widget"}
    if "cloudflare_script" in signals and _CF_TITLE.fullmatch(title.strip()):
        return {"type": "cloudflare", "reason": "cloudflare_challenge_page"}
    if "captcha_script" in signals and status in {403, 429}:
        return {"type": "captcha", "reason": "captcha_script_on_blocked_page"}
    return None
