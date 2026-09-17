from su_crawler.discovery import discover
from su_crawler.models import Source


def test_sitemap_candidates_are_bounded_and_not_prices(tmp_path):
    path = tmp_path / "sitemap.xml"
    path.write_text('<?xml version="1.0"?><urlset><url><loc>https://example.com/a</loc></url><url><loc>https://other.com/b</loc></url><url><loc>https://example.com/c</loc></url></urlset>')
    source = Source("s", "s", "file", str(path), ["p"], allowed_domains=["example.com"])
    result = discover(source, str(tmp_path), limit=1)
    assert result["urls"] == ["https://example.com/a"]
    assert result["limit_reached"]
    assert "price" not in result
