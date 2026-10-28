"""Tests for explicit literature-search provider outcomes and cache semantics."""

from __future__ import annotations

import json
import threading
import urllib.error

import pytest

from ctxflow import CtxFlow
from researchos.agents.retriever import RetrieverAgent
from researchos.skills.literature_search import (
    LiteratureSearchSkill,
    PaperResult,
    ProviderOutcome,
    ProviderParseError,
    SearchOutcome,
    SearchStatus,
    _cache_key,
    search_arxiv,
    search_arxiv_outcome,
    search_semantic_scholar_outcome,
)


ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v5</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on complex
    recurrent or convolutional neural networks.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
</feed>
"""

EMPTY_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""

S2_JSON = {
    "total": 1,
    "data": [
        {
            "paperId": "s2-paper",
            "title": "Attention Is All You Need",
            "abstract": "A transformer architecture.",
            "year": 2017,
            "authors": [{"name": "Ashish Vaswani"}],
            "externalIds": {"DOI": "10.1000/xyz", "ArXiv": "1706.03762"},
            "url": "https://www.semanticscholar.org/paper/s2-paper",
        }
    ],
}


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(payload: bytes):
    def _open(url, timeout=None, **kwargs):
        return FakeResponse(payload)

    return _open


def paper(title: str, source: str = "arxiv") -> PaperResult:
    return PaperResult(title=title, abstract=f"{title} abstract", source=source)


def outcome(
    provider: str,
    status: SearchStatus,
    results=None,
    error: str = "",
) -> ProviderOutcome:
    return ProviderOutcome(
        provider=provider,
        status=status,
        results=list(results or []),
        error=error,
        error_type="RuntimeError" if error else "",
    )


# ---------------------------------------------------------------------------
# Low-level provider outcomes
# ---------------------------------------------------------------------------

class TestArxivOutcome:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(ARXIV_XML.encode())
        )

        result = search_arxiv_outcome("attention", max_results=1)

        assert result.status is SearchStatus.OK
        assert len(result.results) == 1
        assert result.results[0].title == "Attention Is All You Need"
        assert result.results[0].arxiv_id == "1706.03762v5"
        assert result.results[0].source == "arxiv"
        assert result.error == ""

    def test_valid_empty_feed_is_empty(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(EMPTY_ARXIV_XML.encode())
        )

        result = search_arxiv_outcome("nothing")

        assert result.status is SearchStatus.EMPTY
        assert result.results == []
        assert result.error == ""

    def test_malformed_xml_is_error_not_empty(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(b"<feed><entry>")
        )

        result = search_arxiv_outcome("attention")

        assert result.status is SearchStatus.ERROR
        assert result.results == []
        assert result.error_type == ProviderParseError.__name__
        assert "valid XML" in result.error

    def test_network_failure_is_error(self, monkeypatch):
        def fail(url, timeout=None, **kwargs):
            raise urllib.error.URLError("network down")

        monkeypatch.setattr("urllib.request.urlopen", fail)

        result = search_arxiv_outcome("attention")

        assert result.status is SearchStatus.ERROR
        assert result.error_type == "URLError"
        assert "network down" in result.error

    def test_legacy_wrapper_returns_list(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(ARXIV_XML.encode())
        )

        results = search_arxiv("attention")

        assert isinstance(results, list)
        assert results[0].title == "Attention Is All You Need"


class TestSemanticScholarOutcome:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            fake_urlopen(json.dumps(S2_JSON).encode()),
        )

        result = search_semantic_scholar_outcome("attention")

        assert result.status is SearchStatus.OK
        assert result.results[0].doi == "10.1000/xyz"
        assert result.results[0].arxiv_id == "1706.03762"
        assert result.results[0].source == "semantic_scholar"

    def test_empty_data_is_empty(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            fake_urlopen(json.dumps({"total": 0, "data": []}).encode()),
        )

        result = search_semantic_scholar_outcome("nothing")

        assert result.status is SearchStatus.EMPTY
        assert result.results == []

    def test_malformed_json_is_error(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(b"not json")
        )

        result = search_semantic_scholar_outcome("attention")

        assert result.status is SearchStatus.ERROR
        assert result.error_type == "JSONDecodeError"

    def test_non_object_payload_is_error(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(b'["unexpected"]')
        )

        result = search_semantic_scholar_outcome("attention")

        assert result.status is SearchStatus.ERROR
        assert result.error_type == ProviderParseError.__name__

    def test_http_error_captures_status_code(self, monkeypatch):
        def fail(url, timeout=None, **kwargs):
            raise urllib.error.HTTPError(
                url, 429, "Too Many Requests", {}, None
            )

        monkeypatch.setattr("urllib.request.urlopen", fail)

        result = search_semantic_scholar_outcome("attention")

        assert result.status is SearchStatus.ERROR
        assert result.status_code == 429
        assert result.error_type == "HTTPError"


# ---------------------------------------------------------------------------
# Aggregate outcomes and caching
# ---------------------------------------------------------------------------

class TestSearchOutcomeAggregation:
    def test_partial_when_one_provider_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen(ARXIV_XML.encode())
        )
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))

        def fail_s2(*args, **kwargs):
            return outcome(
                "semantic_scholar",
                SearchStatus.ERROR,
                error="rate limited",
            )

        monkeypatch.setattr(
            "researchos.skills.literature_search.search_semantic_scholar_outcome",
            fail_s2,
        )
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_openalex_outcome",
            lambda *a, **k: outcome("openalex", SearchStatus.EMPTY),
        )

        result = skill.search_with_outcome("attention", provider="both")
        skill.close()

        assert result.status is SearchStatus.PARTIAL
        assert len(result.results) == 1
        assert result.errors == {"semantic_scholar": "rate limited"}
        assert result.to_dict()["providers"]["semantic_scholar"]["status"] == "error"

    def test_all_providers_failing_is_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome",
            lambda *a, **k: outcome("arxiv", SearchStatus.ERROR, error="boom"),
        )
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_semantic_scholar_outcome",
            lambda *a, **k: outcome(
                "semantic_scholar", SearchStatus.ERROR, error="rate limited"
            ),
        )
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_openalex_outcome",
            lambda *a, **k: outcome("openalex", SearchStatus.ERROR, error="quota"),
        )
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))

        result = skill.search_with_outcome("attention", provider="both")
        legacy = skill.search("attention", provider="both")
        skill.close()

        assert result.status is SearchStatus.ERROR
        assert result.results == []
        assert result.errors == {
            "arxiv": "boom",
            "semantic_scholar": "rate limited",
            "openalex": "quota",
        }
        # Backward-compatible search() stays list-shaped and does not raise.
        assert legacy == []

    def test_all_empty_is_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome",
            lambda *a, **k: outcome("arxiv", SearchStatus.EMPTY),
        )
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_semantic_scholar_outcome",
            lambda *a, **k: outcome("semantic_scholar", SearchStatus.EMPTY),
        )
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_openalex_outcome",
            lambda *a, **k: outcome("openalex", SearchStatus.EMPTY),
        )
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))

        result = skill.search_with_outcome("attention", provider="both")
        skill.close()

        assert result.status is SearchStatus.EMPTY
        assert result.results == []
        assert result.errors == {}

    def test_unsupported_provider_is_error(self, tmp_path):
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))

        result = skill.search_with_outcome("attention", provider="nope")
        legacy = skill.search("attention", provider="none")
        skill.close()

        assert result.status is SearchStatus.ERROR
        assert "unsupported provider" in result.outcomes[0].error
        assert legacy == []

    def test_error_is_not_cached(self, monkeypatch, tmp_path):
        calls = []
        responses = [
            outcome("arxiv", SearchStatus.ERROR, error="temporary"),
            outcome("arxiv", SearchStatus.OK, results=[paper("Recovered")]),
        ]

        def fake(*args, **kwargs):
            calls.append(1)
            return responses[len(calls) - 1]

        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome", fake
        )
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))

        first = skill.search_with_outcome("attention", provider="arxiv")
        second = skill.search_with_outcome("attention", provider="arxiv")
        skill.close()

        assert first.status is SearchStatus.ERROR
        assert second.status is SearchStatus.OK
        assert [p.title for p in second.results] == ["Recovered"]
        assert len(calls) == 2

    def test_success_is_cached(self, monkeypatch, tmp_path):
        calls = []

        def fake(*args, **kwargs):
            calls.append(1)
            return outcome("arxiv", SearchStatus.OK, results=[paper("Cached")])

        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome", fake
        )
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))

        first = skill.search_with_outcome("attention", provider="arxiv")
        second = skill.search_with_outcome("attention", provider="arxiv")
        skill.close()

        assert first.outcomes[0].from_cache is False
        assert second.outcomes[0].from_cache is True
        assert second.outcomes[0].status is SearchStatus.OK
        assert [p.title for p in second.results] == ["Cached"]
        assert len(calls) == 1

    def test_legacy_cache_list_is_read(self, tmp_path):
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))
        key = _cache_key("arxiv", "attention", 5)
        legacy = [{
            "title": "Legacy Paper",
            "authors": [],
            "abstract": "",
            "year": 2020,
            "doi": "",
            "arxiv_id": "2001.00001",
            "url": "",
            "source": "arxiv",
            "metadata": {},
        }]
        skill._cache.set(key, json.dumps(legacy))

        result = skill.search_with_outcome("attention", provider="arxiv")
        skill.close()

        assert result.status is SearchStatus.OK
        assert result.outcomes[0].from_cache is True
        assert result.results[0].title == "Legacy Paper"


# ---------------------------------------------------------------------------
# Retriever wiring
# ---------------------------------------------------------------------------

class FakeOutcomeSkill:
    def __init__(self, result: SearchOutcome):
        self.result = result

    def search_with_outcome(self, query, max_results=5):
        return self.result


class FakeLegacySkill:
    def search(self, query, max_results=5):
        return [paper("Legacy Result")]


class TestRetrieverOutcomeWiring:
    def test_retriever_exposes_provider_failure(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "force")
        cf = CtxFlow()
        failure = SearchOutcome(
            query="attention",
            provider="both",
            outcomes=[
                outcome("arxiv", SearchStatus.ERROR, error="upstream 503"),
                outcome("semantic_scholar", SearchStatus.EMPTY),
            ],
            results=[],
            status=SearchStatus.PARTIAL,
        )
        retriever = RetrieverAgent(
            ctxflow=cf,
            literature_search=FakeOutcomeSkill(failure),
            min_internal_results=1,
        )

        result = retriever.run(query="attention")

        assert result.success is True
        assert result.data["external_results"] == []
        assert result.data["external_search"]["status"] == "partial"
        assert result.data["external_search"]["errors"] == {
            "arxiv": "upstream 503"
        }
        assert result.metadata["external_search_status"] == "partial"
        assert result.metadata["external_errors"] == {"arxiv": "upstream 503"}

    def test_retriever_keeps_legacy_skill_compatibility(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "force")
        cf = CtxFlow()
        retriever = RetrieverAgent(
            ctxflow=cf,
            literature_search=FakeLegacySkill(),
            min_internal_results=1,
        )

        result = retriever.run(query="attention")

        assert result.success is True
        assert result.data["external_results"][0]["title"] == "Legacy Result"
        assert result.data["external_search"]["status"] == "unknown"
        assert result.metadata["external_search_status"] == "unknown"


# ---------------------------------------------------------------------------
# Dedupe: untitled results are no longer dropped
# ---------------------------------------------------------------------------

class TestDedupe:
    def test_untitled_result_is_kept(self):
        kept = LiteratureSearchSkill._dedupe(
            [PaperResult(title=""), PaperResult(title="Real Paper")]
        )
        assert [p.title for p in kept] == ["", "Real Paper"]

    def test_all_untitled_results_are_kept(self):
        kept = LiteratureSearchSkill._dedupe(
            [PaperResult(title=""), PaperResult(title="   ")]
        )
        assert len(kept) == 2

    def test_untitled_results_dedupe_by_doi(self):
        kept = LiteratureSearchSkill._dedupe(
            [
                PaperResult(title="", doi="10.1000/x"),
                PaperResult(title="", doi="10.1000/X"),
            ]
        )
        assert len(kept) == 1

    def test_titles_still_dedupe_case_insensitively(self):
        kept = LiteratureSearchSkill._dedupe(
            [PaperResult(title="Attention Is All You Need"), PaperResult(title=" attention is all you need ")]
        )
        assert len(kept) == 1


# ---------------------------------------------------------------------------
# Cache robustness
# ---------------------------------------------------------------------------

class TestCacheRobustness:
    def test_unknown_cached_status_forces_refetch(self, monkeypatch, tmp_path):
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))
        skill._cache.set(
            _cache_key("arxiv", "q", 5),
            json.dumps({"status": "bogus", "results": []}),
        )
        fetched = []

        def fake_fetch(provider, query, max_results):
            fetched.append(provider)
            return ProviderOutcome(
                provider=provider,
                status=SearchStatus.OK,
                results=[paper("Fresh Paper")],
            )

        monkeypatch.setattr(
            LiteratureSearchSkill, "_fetch_outcome", staticmethod(fake_fetch)
        )

        outcome = skill.search_with_outcome("q", provider="arxiv")
        skill.close()

        assert fetched == ["arxiv"]
        assert outcome.status is SearchStatus.OK
        assert outcome.results[0].title == "Fresh Paper"

    def test_cache_is_usable_from_worker_threads(self, tmp_path):
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))
        skill._cache.set("k", "v")
        seen = {}
        errors = []

        def worker():
            try:
                seen["value"] = skill._cache.get("k")
                skill._cache.set("k2", "v2")
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert errors == []
        assert seen["value"] == "v"
        assert skill._cache.get("k2") == "v2"
        skill.close()

    def test_search_with_outcome_works_off_the_main_thread(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome",
            lambda *a, **k: ProviderOutcome(
                provider="arxiv", status=SearchStatus.OK, results=[paper("T")]
            ),
        )
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path))
        errors = []
        statuses = []

        def worker():
            try:
                statuses.append(skill.search_with_outcome("q", provider="arxiv").status)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        skill.close()

        assert errors == []
        assert statuses == [SearchStatus.OK]
