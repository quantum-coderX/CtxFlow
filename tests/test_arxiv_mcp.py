"""Tests for the MCP-backed arXiv search client and its fallback path.

No test spawns ``uvx``: the parsing, configuration, and
``LiteratureSearchSkill`` fallback contracts are covered with fakes. The live
smoke test is opt-in via ``RESEARCHOS_LIVE_MCP=1``.
"""

from __future__ import annotations

import json
import os

import pytest

from researchos.mcp import ArxivMCPClient, MCPUnavailable, parse_search_papers_result
from researchos.skills.literature_search import (
    LiteratureSearchSkill,
    PaperResult,
    ProviderOutcome,
    SearchStatus,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, payload, *, is_error: bool = False) -> None:
        if isinstance(payload, str):
            self.content = [_Block(payload)]
        else:
            self.content = [_Block(json.dumps(payload))]
        self.isError = is_error


PAYLOAD = {
    "returned": 2,
    "papers": [
        {
            "id": "2405.16506",
            "versioned_id": "2405.16506v3",
            "title": "GRAG:  Graph Retrieval-Augmented Generation",
            "authors": ["Yuntong Hu", "Liang Zhao"],
            "abstract": "Naive RAG retrieves documents one at a time.",
            "categories": ["cs.LG"],
            "published": "2024-05-26T10:11:40Z",
            "url": "https://arxiv.org/pdf/2405.16506v3",
        },
        {
            "id": "2310.08560",
            "versioned_id": "2310.08560v2",
            "title": "MemGPT: Towards LLMs as Operating Systems",
            "authors": ["Charles Packer"],
            "abstract": "Large language models are limited by context windows\u2026 [truncated]",
            "categories": ["cs.AI"],
            "published": "2023-10-12T00:00:00Z",
            "url": "https://arxiv.org/pdf/2310.08560v2",
        },
    ],
}


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

class TestParseSearchPapers:
    def test_parses_titles_ids_years_and_abstracts(self):
        papers = parse_search_papers_result(_Result(PAYLOAD))

        assert [p["arxiv_id"] for p in papers] == ["2405.16506", "2310.08560"]
        assert papers[0]["title"] == "GRAG: Graph Retrieval-Augmented Generation"
        assert papers[0]["year"] == 2024
        assert papers[1]["year"] == 2023
        assert papers[1]["abstract"].endswith("context windows\u2026")
        assert papers[0]["categories"] == ["cs.LG"]

    def test_accepts_a_bare_list(self):
        papers = parse_search_papers_result(_Result(PAYLOAD["papers"]))
        assert len(papers) == 2

    def test_tool_error_raises(self):
        with pytest.raises(MCPUnavailable, match="search_papers failed"):
            parse_search_papers_result(_Result("boom", is_error=True))

    def test_non_json_raises(self):
        with pytest.raises(MCPUnavailable, match="non-JSON"):
            parse_search_papers_result(_Result("not json at all"))

    def test_empty_content_raises(self):
        result = _Result({"papers": []})
        result.content = []
        with pytest.raises(MCPUnavailable, match="no content"):
            parse_search_papers_result(result)


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------

class TestClientConfig:
    def test_disabled_client_never_spawns(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")
        client = ArxivMCPClient()
        assert client.enabled is False
        with pytest.raises(MCPUnavailable, match="disabled"):
            client.search("anything")
        assert client.available is None  # never started

    def test_args_can_be_configured_with_json(self, monkeypatch):
        monkeypatch.setenv(
            "RESEARCHOS_MCP_ARGS", '["--from", "arxiv-mcp-server", "arxiv-mcp-server"]'
        )
        client = ArxivMCPClient(enabled=False)
        assert client.args == ["--from", "arxiv-mcp-server", "arxiv-mcp-server"]

    def test_zero_results_returns_empty_list(self, monkeypatch):
        client = ArxivMCPClient(enabled=False)
        # max_results <= 0 short-circuits before touching the transport.
        assert client.search("anything", max_results=0) == []


# ---------------------------------------------------------------------------
# LiteratureSearchSkill integration / fallback
# ---------------------------------------------------------------------------

class _FakeMCP:
    def __init__(self, papers=None, error=None) -> None:
        self.papers = papers or []
        self.error = error
        self.calls = []

    def search(self, query, max_results=5):
        self.calls.append((query, max_results))
        if self.error is not None:
            raise self.error
        return list(self.papers)


def _http_outcome():
    return ProviderOutcome(
        provider="arxiv",
        status=SearchStatus.OK,
        results=[PaperResult(title="HTTP paper", arxiv_id="1234.5678", source="arxiv")],
    )


class TestSearchSkillFallback:
    def test_mcp_results_are_used_when_available(self, tmp_path, monkeypatch):
        paper = PaperResult(
            title="MCP paper",
            arxiv_id="2405.16506",
            source="arxiv-mcp",
        )
        client = _FakeMCP([paper])
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path), arxiv_client=client)

        called = {"http": False}

        def fail_http(*args, **kwargs):
            called["http"] = True
            return _http_outcome()

        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome", fail_http
        )

        outcome = skill.search_with_outcome("graph rag", provider="arxiv")
        skill.close()

        assert outcome.status is SearchStatus.OK
        assert outcome.results[0].title == "MCP paper"
        assert outcome.outcomes[0].transport == "mcp"
        assert called["http"] is False
        assert client.calls == [("graph rag", 5)]
        assert skill.transport_stats == {
            "mcp_calls": 1,
            "http_fallbacks": 0,
            "last_mcp_error": None,
        }

    def test_mcp_failure_falls_back_to_http(self, tmp_path, monkeypatch):
        client = _FakeMCP(error=RuntimeError("uvx not found"))
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path), arxiv_client=client)

        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome",
            lambda *a, **k: _http_outcome(),
        )

        outcome = skill.search_with_outcome("graph rag", provider="arxiv")
        skill.close()

        assert outcome.status is SearchStatus.OK
        assert outcome.results[0].title == "HTTP paper"
        assert outcome.outcomes[0].transport == "http"
        assert skill.transport_stats["http_fallbacks"] == 1
        assert "uvx not found" in skill.transport_stats["last_mcp_error"]

    def test_empty_mcp_page_is_empty_not_error(self, tmp_path, monkeypatch):
        client = _FakeMCP([])
        skill = LiteratureSearchSkill(cache_dir=str(tmp_path), arxiv_client=client)

        def fail_http(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("HTTP fallback should not run for a valid empty page")

        monkeypatch.setattr(
            "researchos.skills.literature_search.search_arxiv_outcome", fail_http
        )

        outcome = skill.search_with_outcome("nothing matches", provider="arxiv")
        skill.close()

        assert outcome.status is SearchStatus.EMPTY
        assert outcome.outcomes[0].transport == "mcp"


# ---------------------------------------------------------------------------
# Opt-in live smoke test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("RESEARCHOS_LIVE_MCP") != "1",
    reason="set RESEARCHOS_LIVE_MCP=1 to spawn uvx arxiv-mcp-server",
)
def test_live_mcp_arxiv_search():
    client = ArxivMCPClient(enabled=True, startup_timeout=240.0, call_timeout=90.0)
    try:
        papers = client.search("graph retrieval augmented generation", max_results=2)
    finally:
        client.close()

    assert papers
    assert papers[0].source == "arxiv-mcp"
    assert papers[0].title
    assert papers[0].arxiv_id
    assert len(papers[0].abstract) > 100
