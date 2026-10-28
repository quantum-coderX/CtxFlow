"""Offline tests for the web search skill and the bounded page fetcher."""

from __future__ import annotations

import httpx
import pytest

from researchos.skills.literature_search import SearchStatus
from researchos.skills.source_fetch import (
    FetchStatus,
    SourceFetchSkill,
    canonical_url,
    check_public_url,
    extract_readable_text,
)
from researchos.skills.web_search import WebSearchSkill, web_search_enabled

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 — fake public resolver

ARTICLE = (
    "<html><head><title>Graph Memory</title><script>var x=1;</script></head><body>"
    "<nav>Home About Contact us today for more</nav>"
    "<article><h1>Graph memory for agents</h1>"
    "<p>Graph-based memory systems store entities and relations so that agents can "
    "retrieve evidence across long horizons without rereading every document.</p>"
    "<p>Vector retrieval alone loses the relational structure between claims, which "
    "limits multi-hop questions over large corpora.</p></article>"
    "<footer>Copyright</footer></body></html>"
)


def _skill(handler, **kwargs):
    kwargs.setdefault("min_host_interval", 0)
    kwargs.setdefault("respect_robots", False)
    return SourceFetchSkill(
        transport=httpx.MockTransport(handler), resolver=PUBLIC, **kwargs
    )


def _html(body: str = ARTICLE, status: int = 200, ctype: str = "text/html; charset=utf-8"):
    return httpx.Response(status, text=body, headers={"content-type": ctype})


# -- web search --------------------------------------------------------------

class FakeProvider:
    name = "fake"

    def __init__(self, hits=None, error=None):
        self.hits, self.error, self.calls = hits, error, []

    def search(self, query, max_results):
        self.calls.append((query, max_results))
        if self.error:
            raise self.error
        return self.hits


def test_web_search_ok_empty_and_error_are_distinct():
    ok = WebSearchSkill(
        FakeProvider([{"title": "T", "url": "https://a.example/x", "snippet": "s"}]),
        enabled=True,
    ).search_with_outcome("graph memory")
    empty = WebSearchSkill(FakeProvider([]), enabled=True).search_with_outcome("q")
    error = WebSearchSkill(FakeProvider(error=TimeoutError("slow")), enabled=True).search_with_outcome("q")

    assert (ok.status, empty.status, error.status) == (
        SearchStatus.OK, SearchStatus.EMPTY, SearchStatus.ERROR,
    )
    assert ok.results[0].url == "https://a.example/x"
    assert error.error_type == "TimeoutError"


def test_web_search_caps_results_dedupes_and_rejects_bad_urls():
    hits = [
        {"title": "A", "url": "https://a.example/1"},
        {"title": "A again", "url": "https://a.example/1"},
        {"title": "bad", "url": "javascript:alert(1)"},
        {"title": "B", "url": "https://b.example/2"},
        {"title": "C", "url": "https://c.example/3"},
    ]
    provider = FakeProvider(hits)
    outcome = WebSearchSkill(provider, enabled=True).search_with_outcome("q", max_results=2)

    assert [r.url for r in outcome.results] == ["https://a.example/1", "https://b.example/2"]
    assert provider.calls == [("q", 2)]


def test_web_search_malformed_response_is_an_error():
    outcome = WebSearchSkill(FakeProvider({"not": "a list"}), enabled=True).search_with_outcome("q")
    assert outcome.status is SearchStatus.ERROR
    assert outcome.error_type == "MalformedResponse"

    only_junk = WebSearchSkill(FakeProvider(["junk"]), enabled=True).search_with_outcome("q")
    assert only_junk.status is SearchStatus.ERROR


def test_web_search_is_off_by_default_and_follows_external_switch(monkeypatch):
    monkeypatch.delenv("RESEARCHOS_WEB_SEARCH", raising=False)
    monkeypatch.delenv("RESEARCHOS_EXTERNAL_SEARCH", raising=False)
    assert web_search_enabled() is False

    monkeypatch.setenv("RESEARCHOS_WEB_SEARCH", "on")
    assert web_search_enabled() is True

    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "off")
    assert web_search_enabled() is False
    provider = FakeProvider([])
    outcome = WebSearchSkill(provider).search_with_outcome("q")
    assert outcome.status is SearchStatus.ERROR and provider.calls == []


# -- URL safety --------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://printer.local/",
        "http://intranet/",
        "https://user:pass@example.com/",
        "https://example.com:8443/",
    ],
)
def test_unsafe_urls_are_refused(url):
    assert check_public_url(url, PUBLIC)


def test_hostname_resolving_to_private_address_is_refused():
    assert check_public_url("https://evil.example/", lambda h: ["10.1.2.3"])
    assert check_public_url("https://ok.example/", PUBLIC) is None


def test_fetch_blocks_private_destination_without_a_request():
    calls = []
    skill = _skill(lambda request: calls.append(request) or _html())
    outcome = skill.fetch("http://127.0.0.1/secret")

    assert outcome.status is FetchStatus.BLOCKED
    assert calls == []


def test_redirect_to_private_address_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    outcome = _skill(handler).fetch("https://ok.example/start")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_type == "BlockedURL"


def test_redirect_chain_is_bounded():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://ok.example/again"})

    outcome = _skill(handler, max_redirects=2).fetch("https://ok.example/start")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_type == "TooManyRedirects"


def test_redirects_are_followed_and_final_url_recorded():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/final"})
        return _html()

    outcome = _skill(handler).fetch("https://ok.example/start")
    assert outcome.status is FetchStatus.OK
    assert outcome.final_url == "https://ok.example/final"


# -- fetch limits and extraction ---------------------------------------------

def test_fetch_extracts_readable_text_and_title():
    outcome = _skill(lambda r: _html()).fetch("https://ok.example/a")

    assert outcome.status is FetchStatus.OK
    assert outcome.title == "Graph Memory"
    assert "relational structure" in outcome.text
    assert "var x" not in outcome.text
    assert "Copyright" not in outcome.text
    assert "Home About" not in outcome.text
    assert "text" not in outcome.to_dict()  # bodies are never serialized


def test_fetch_rejects_oversized_response():
    big = "<html><body><p>" + "word " * 5000 + "</p></body></html>"
    outcome = _skill(lambda r: _html(big), max_bytes=1000).fetch("https://ok.example/big")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_type == "TooLarge"


def test_fetch_rejects_declared_oversize_before_reading():
    def handler(request):
        return httpx.Response(
            200, content=b"x", headers={"content-type": "text/html", "content-length": "99999999"}
        )

    outcome = _skill(handler, max_bytes=1000).fetch("https://ok.example/big")
    assert outcome.error_type == "TooLarge"


def test_fetch_rejects_unreadable_content_types():
    outcome = _skill(lambda r: _html("%PDF-1.4", ctype="application/pdf")).fetch("https://ok.example/a.pdf")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_type == "ContentType"


def test_fetch_reports_http_errors_and_empty_pages():
    assert _skill(lambda r: _html("nope", status=404)).fetch("https://ok.example/").error_type == "HTTPStatus"
    empty = _skill(lambda r: _html("<html><body><script>1</script></body></html>")).fetch("https://ok.example/")
    assert empty.status is FetchStatus.ERROR and empty.error_type == "NotReadable"


def test_fetch_timeout_is_an_error_outcome():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    outcome = _skill(handler).fetch("https://ok.example/")
    assert outcome.status is FetchStatus.ERROR and outcome.error_type == "Timeout"


def test_robots_disallow_blocks_fetch():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return _html()

    skill = _skill(handler, respect_robots=True)
    blocked = skill.fetch("https://ok.example/private/page")
    allowed = skill.fetch("https://ok.example/public")

    assert blocked.error_type == "RobotsDisallowed"
    assert allowed.status is FetchStatus.OK
    assert seen.count("/robots.txt") == 1  # cached per origin


def test_extract_readable_text_prefers_article_content():
    title, text = extract_readable_text(ARTICLE)
    assert title == "Graph Memory"
    assert text.startswith("Graph memory for agents")


def test_canonical_url_ignores_tracking_fragments_and_www():
    a = canonical_url("http://www.Example.com/path/?utm_source=x&b=2&a=1#frag")
    b = canonical_url("https://example.com/path?a=1&b=2")
    assert a == b
