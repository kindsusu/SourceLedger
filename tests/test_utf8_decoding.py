from su_crawler.adapters import common
from su_crawler.adapters.common import html_soup

KOREAN_TITLE = "신차 장기렌트 견적"


class _MisreadingDetector:
    """Stands in for statistical charset detection that guesses cp1252 for undeclared pages."""

    def __init__(self, content, is_html=False):
        self.unicode_markup = content.decode("cp1252", errors="replace")


def test_utf8_page_without_charset_declaration_decodes_as_utf8(monkeypatch):
    monkeypatch.setattr(common, "UnicodeDammit", _MisreadingDetector)
    page = f"<html><body><h1>{KOREAN_TITLE}</h1><p>월 렌트비 안내</p></body></html>"
    soup = html_soup(page.encode("utf-8"))
    assert soup.h1.get_text() == KOREAN_TITLE
    assert "월 렌트비 안내" in soup.get_text()


def test_utf8_page_with_bom_drops_the_bom():
    soup = html_soup(b"\xef\xbb\xbf" + "<html><body><p>견적</p></body></html>".encode("utf-8"))
    assert soup.p.get_text() == "견적"
    assert "\ufeff" not in soup.get_text()


def test_cp949_page_with_charset_meta_still_decodes_via_the_fallback():
    page = f'<html><head><meta charset="euc-kr"></head><body><h1>{KOREAN_TITLE}</h1></body></html>'
    content = page.encode("cp949")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("fixture must not be valid UTF-8")
    assert html_soup(content).h1.get_text() == KOREAN_TITLE
