"""Offline tests for the host-browser capture policy guard; no DNS or network access."""
import pytest

from su_crawler.host_capture_policy import check_host_capture

APPROVED = "https://shop.example.test/item"
PAGE = {"url": APPROVED, "status": "blocked"}


def robots(decision, reason="test"):
    calls = []

    def check(url):
        calls.append(url)
        return decision, reason
    check.calls = calls
    return check


def test_same_url_is_accepted_without_robots_lookup():
    check = robots("disallowed")
    record = check_host_capture(PAGE, APPROVED, robots_check=check)
    assert record["redirected"] is False and record["robots"] == "not_needed"
    assert record["approved_url"] == record["final_url"] == APPROVED
    assert not check.calls
    assert check_host_capture(PAGE, APPROVED, APPROVED + "#top", robots_check=check)["redirected"] is False


@pytest.mark.parametrize("status", ["blocked", "visited", "no_data"])
def test_answered_page_statuses_need_no_new_robots_decision(status):
    check = robots("disallowed")
    record = check_host_capture({"url": APPROVED, "status": status}, APPROVED, robots_check=check)
    assert record["redirected"] is False and record["robots"] == "not_needed" and not check.calls


def test_robots_unavailable_failure_cannot_be_backfilled_without_robots_allow():
    # The collector records robots.txt trouble as a plain failure, not policy_denied.
    page = {"url": APPROVED, "status": "failed", "reason": "robots.txt access is unavailable"}
    with pytest.raises(ValueError, match="could not be verified"):
        check_host_capture(page, APPROVED, robots_check=robots("unavailable", "robots.txt access is unavailable"))
    check = robots("allowed")
    assert check_host_capture(page, APPROVED, robots_check=check)["robots"] == "allowed"
    assert check.calls == [APPROVED]


def test_omitted_final_url_is_recorded_as_unreported():
    assert check_host_capture(PAGE, APPROVED)["final_url_reported"] is False
    assert check_host_capture(PAGE, APPROVED, APPROVED)["final_url_reported"] is True


@pytest.mark.parametrize("status", ["unprocessed", "interrupted", "timeout", "failed", "tool_unavailable"])
def test_unproven_page_needs_robots_decision_even_without_redirect(status):
    page = {"url": APPROVED, "status": status}
    check = robots("allowed")
    record = check_host_capture(page, APPROVED, robots_check=check)
    assert check.calls == [APPROVED] and record["robots"] == "allowed"
    with pytest.raises(ValueError, match="disallowed by robots"):
        check_host_capture(page, APPROVED, robots_check=robots("disallowed"))
    with pytest.raises(ValueError, match="could not be verified"):
        check_host_capture(page, APPROVED, robots_check=robots("unavailable"))


def test_subdomain_redirect_is_accepted_when_robots_allows():
    check = robots("allowed")
    record = check_host_capture(PAGE, APPROVED, "https://www.shop.example.test/item/1?x=1#frag", robots_check=check)
    assert record["redirected"] is True and record["robots"] == "allowed" and "robots_allowed" in record["checks"]
    assert record["final_url"] == "https://www.shop.example.test/item/1?x=1"
    assert check.calls == [record["final_url"]]


@pytest.mark.parametrize("final, message", [
    ("https://other.example.test/item", "approved page's host"),
    ("https://evilshop.example.test/item", "approved page's host"),
    ("https://user:secret@shop.example.test/item", "credentials"),
    ("https://@shop.example.test/item", "credentials"),
    ("ftp://shop.example.test/item", "http"),
    ("file:///etc/passwd", "http"),
    ("javascript:alert(1)", "http"),
    ("https:///item", "http"),
    ("", "non-empty"),
])
def test_unsafe_final_urls_are_rejected(final, message):
    check = robots("allowed")
    with pytest.raises(ValueError, match=message):
        check_host_capture(PAGE, APPROVED, final, robots_check=check)
    assert not check.calls


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "[::1]", "[fd00::1]"])
def test_private_ip_literal_final_is_rejected(ip):
    with pytest.raises(ValueError):
        check_host_capture(PAGE, APPROVED, f"https://{ip}/item", robots_check=robots("allowed"))


def test_private_ip_literal_rejected_even_when_page_was_approved_on_it():
    with pytest.raises(ValueError, match="private"):
        check_host_capture({"url": "https://10.0.0.5/item", "status": "blocked"}, "https://10.0.0.5/item",
                           robots_check=robots("allowed"))


def test_policy_denied_page_cannot_be_backfilled():
    with pytest.raises(ValueError, match="denied by collection policy"):
        check_host_capture({"url": APPROVED, "status": "policy_denied"}, APPROVED)


def test_redirect_with_robots_disallowed_is_rejected():
    with pytest.raises(ValueError, match="disallowed by robots"):
        check_host_capture(PAGE, APPROVED, "https://www.shop.example.test/private", robots_check=robots("disallowed"))


def test_redirect_with_unverifiable_robots_is_rejected():
    with pytest.raises(ValueError, match="could not be verified"):
        check_host_capture(PAGE, APPROVED, "https://www.shop.example.test/other",
                           robots_check=robots("unavailable", "Unable to check robots.txt"))


def test_live_robots_check_uses_collector_helpers_without_network(monkeypatch):
    from su_crawler import collectors
    seen = {}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def decision(client, source, url, cache, timeout):
        seen.update(client=client, domains=source.allowed_domains, url=url, cache=cache)
        return "allowed", "ok"
    monkeypatch.setattr(collectors, "_new_http_client", lambda timeout: Client())
    monkeypatch.setattr(collectors, "_robots_decision", decision)
    record = check_host_capture(PAGE, APPROVED, "https://www.shop.example.test/item")
    assert record["robots"] == "allowed"
    assert seen["domains"] == ["shop.example.test"] and seen["cache"] is None
    assert isinstance(seen["client"], Client)
