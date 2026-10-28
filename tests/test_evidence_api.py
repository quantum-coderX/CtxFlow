"""API and pipeline tests for explicit external-search outcomes.

These cover the consumer side of the evidence work: the status has to reach
``/api/search``, ``/api/chat`` and the SSE stream, and a provider outage must
not be reported as a successful retrieval.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from researchos.agents.base import AgentResult
from researchos.agents.critic import CriticAgent
from researchos.agents.retriever import RetrieverAgent
from researchos.agents.router import SkillRouter
from researchos.api.server import create_app
from researchos.skills.literature_search import (
    LiteratureSearchSkill,
    PaperResult,
    ProviderOutcome,
    SearchOutcome,
    SearchStatus,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLiteratureSearch:
    """Stand-in for LiteratureSearchSkill with a scripted outcome."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def _next(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._outcomes) > 1:
            return self._outcomes.pop(0)
        return self._outcomes[0]

    def search_with_outcome(self, **kwargs):
        return self._next(**kwargs)

    def search(self, **kwargs):
        return list(self._next(**kwargs).results)

    def close(self):
        pass


def failure_outcomes():
    return [
        ProviderOutcome(
            provider="arxiv",
            status=SearchStatus.ERROR,
            error="upstream 503",
            error_type="HTTPError",
            status_code=503,
        ),
        ProviderOutcome(
            provider="semantic_scholar",
            status=SearchStatus.ERROR,
            error="HTTP Error 429: Too Many Requests",
            error_type="HTTPError",
            status_code=429,
        ),
    ]


def error_outcome(query="attention"):
    return SearchOutcome(
        query=query,
        provider="both",
        outcomes=failure_outcomes(),
        results=[],
        status=SearchStatus.ERROR,
        elapsed_ms=12.5,
    )


@pytest.fixture
def app_factory(monkeypatch):
    """Return a builder that swaps LiteratureSearchSkill for a fake."""
    import researchos.api.server as server

    def build(skill):
        monkeypatch.setattr(
            server, "LiteratureSearchSkill", lambda *a, **k: skill
        )
        return create_app()

    return build


# ---------------------------------------------------------------------------
# /api/search
# ---------------------------------------------------------------------------

class TestSearchEndpointOutcomes:
    def test_total_failure_is_reported_as_error(self, app_factory):
        skill = FakeLiteratureSearch([error_outcome()])
        app = app_factory(skill)
        with TestClient(app) as client:
            response = client.post(
                "/api/search", json={"query": "attention", "provider": "both"}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["status"] == "error"
        assert body["provider"] == "both"
        assert body["errors"]["arxiv"] == "upstream 503"
        assert body["errors"]["semantic_scholar"].startswith("HTTP Error 429")
        assert body["providers"]["arxiv"]["status_code"] == 503
        assert body["providers"]["arxiv"]["status"] == "error"
        assert body["elapsed_ms"] == pytest.approx(12.5)

    def test_partial_failure_is_reported_as_partial(self, app_factory):
        outcome = SearchOutcome(
            query="attention",
            provider="both",
            outcomes=[
                ProviderOutcome(
                    provider="arxiv",
                    status=SearchStatus.OK,
                    results=[PaperResult(title="Attention Is All You Need")],
                ),
                failure_outcomes()[1],
            ],
            results=[PaperResult(title="Attention Is All You Need")],
            status=SearchStatus.PARTIAL,
        )
        app = app_factory(FakeLiteratureSearch([outcome]))
        with TestClient(app) as client:
            body = client.post("/api/search", json={"query": "attention"}).json()

        assert body["status"] == "partial"
        assert body["total"] == 1
        assert body["results"][0]["title"] == "Attention Is All You Need"
        assert list(body["errors"]) == ["semantic_scholar"]

    def test_legacy_skill_reports_unknown_status(self, app_factory):
        class LegacyOnly:
            def search(self, **kwargs):
                return [PaperResult(title="Legacy Paper")]

            def close(self):
                pass

        app = app_factory(LegacyOnly())
        with TestClient(app) as client:
            body = client.post("/api/search", json={"query": "anything"}).json()

        assert body["status"] == "unknown"
        assert body["total"] == 1
        assert body["providers"] == {}
        assert body["errors"] == {}


# ---------------------------------------------------------------------------
# /api/chat and the router payload
# ---------------------------------------------------------------------------

class StubRetriever:
    """Agent named ``retriever`` that reports a scripted search outcome."""

    name = "retriever"

    def __init__(self, external_search, success=True, error=None):
        self._payload = external_search
        self._success = success
        self._error = error

    def run(self, **kwargs):
        return AgentResult(
            agent_name=self.name,
            success=self._success,
            error=self._error,
            data={"external_search": self._payload},
        )


class TestChatOutcomeSurface:
    def test_chat_exposes_external_search(self):
        payload = error_outcome().to_dict()
        router_agent = SkillRouter(
            agents={
                "retriever": StubRetriever(
                    payload, success=False, error="External literature search failed."
                )
            }
        )
        app = create_app(router_agent=router_agent)
        with TestClient(app) as client:
            body = client.post(
                "/api/chat", json={"query": "find papers on attention"}
            ).json()

        assert body["external_search"]["status"] == "error"
        assert "arxiv" in body["external_search"]["errors"]

    def test_router_marks_partial_pipeline(self):
        redirect_payload = {
            "status": "skipped",
            "provider": "none",
            "result_count": 0,
            "errors": {},
        }
        router_agent = SkillRouter(
            agents={"retriever": StubRetriever(redirect_payload)}
        )
        result = router_agent.run(query="find papers on attention")

        assert result.data["external_search"] == redirect_payload
        assert result.metadata["external_search_status"] == "skipped"
        assert result.metadata["partial"] is False

    def test_router_fails_when_an_agent_fails(self):
        router_agent = SkillRouter(
            agents={
                "retriever": StubRetriever(
                    error_outcome().to_dict(),
                    success=False,
                    error="External literature search failed.",
                )
            }
        )
        result = router_agent.run(query="find papers on attention")

        assert result.success is False
        assert result.metadata["partial"] is True
        assert result.metadata["failed_agents"] == ["retriever"]
        assert any(
            item["agent"] == "retriever" and item["success"] is False
            for item in result.data["pipeline_results"]
        )


# ---------------------------------------------------------------------------
# Agent-level failure semantics
# ---------------------------------------------------------------------------

class FailingSearchSkill:
    def __init__(self, outcome):
        self._outcome = outcome

    def search_with_outcome(self, query, max_results=5):
        return self._outcome


class TestRetrieverFailureSemantics:
    def test_total_provider_failure_is_a_failed_retrieval(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "force")
        retriever = RetrieverAgent(
            ctxflow=CtxFlow(),
            literature_search=FailingSearchSkill(error_outcome()),
            min_internal_results=99,
        )
        result = retriever.run(query="attention")

        assert result.success is False
        assert "External literature search failed" in result.error
        assert result.metadata["degraded"] is True
        assert result.metadata["external_search_status"] == "error"

    def test_partial_failure_stays_successful_but_degraded(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "force")
        outcome = SearchOutcome(
            query="attention",
            provider="both",
            outcomes=[
                ProviderOutcome(
                    provider="arxiv",
                    status=SearchStatus.OK,
                    results=[PaperResult(title="A Paper")],
                ),
                failure_outcomes()[1],
            ],
            results=[PaperResult(title="A Paper")],
            status=SearchStatus.PARTIAL,
        )
        retriever = RetrieverAgent(
            ctxflow=CtxFlow(),
            literature_search=FailingSearchSkill(outcome),
            min_internal_results=99,
        )
        result = retriever.run(query="attention")

        assert result.success is True
        assert result.metadata["degraded"] is True
        assert result.metadata["external_search_status"] == "partial"
        assert result.data["external_results"][0]["title"] == "A Paper"


class TestCriticEvidence:
    def test_no_evidence_is_not_verified(self):
        critic = CriticAgent(ctxflow=CtxFlow())
        result = critic.run(
            query="an unrelated question with no matching nodes",
            context={"previous_output": {"cited_nodes": [], "answer": ""}},
        )

        assert result.data["verdict"] == "INSUFFICIENT_EVIDENCE"
        assert result.data["verified"] is False
        assert result.data["total_claims_checked"] == 0
        assert any(a["type"] == "insufficient_evidence" for a in result.data["alerts"])


# ---------------------------------------------------------------------------
# SSE: the pipeline runs in a worker thread
# ---------------------------------------------------------------------------

def _ok_provider(provider, title):
    return lambda *args, **kwargs: ProviderOutcome(
        provider=provider,
        status=SearchStatus.OK,
        results=[PaperResult(title=title, abstract="x", source=provider)],
    )


def test_stream_survives_worker_thread(monkeypatch, tmp_path):
    """The SSE endpoint must not crash when running the workflow in a worker thread.

    Previously tested the old SkillRouter.run() path. Now tests that the
    ResearchWorkflow runs in a thread and emits a well-formed 'done' event.
    """
    from researchos.persistence import ResearchWorkspace

    import researchos.skills.literature_search as ls

    monkeypatch.setattr(ls, "search_arxiv_outcome", _ok_provider("arxiv", "Arxiv Paper"))
    monkeypatch.setattr(
        ls, "search_semantic_scholar_outcome", _ok_provider("semantic_scholar", "S2 Paper")
    )

    workspace = ResearchWorkspace(data_dir=tmp_path)
    # Pre-create the project so the workspace is ready
    workspace.create_project("default", name="Default")

    skill = LiteratureSearchSkill(cache_dir=str(tmp_path / "cache"))
    app = create_app(workspace=workspace)
    # Override the literature_search skill in state after creation
    # (done via monkeypatch on the module-level functions above)

    try:
        with TestClient(app) as client:
            with client.stream(
                "GET", "/api/stream", params={"query": "find papers on attention"}
            ) as response:
                assert response.status_code == 200
                done = None
                event = None
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line[len("data:"):].strip())
                        if event == "done":
                            done = payload
                            break

        assert done is not None, "Stream did not emit a 'done' event"
        # New workflow done event has status, answer, verdict (not pipeline_results)
        assert "status" in done, f"done event missing 'status': {done}"
        assert "answer" in done, f"done event missing 'answer': {done}"
        # Secrets must never appear in the stream
        assert "api_key" not in json.dumps(done)
    finally:
        skill.close()
