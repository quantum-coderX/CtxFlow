"""Parallel research workflow: plan -> fan-out -> ingest -> cite -> reuse (offline)."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List

import pytest

from ctxflow import CtxFlow
from researchos.agents.reader import ReaderAgent
from researchos.orchestration.research_tasks import ResearchLimits
from researchos.orchestration.workflow import (
    STAGES,
    ResearchGraph,
    ResearchWorkflow,
    maybe_langgraph_available,
)
from researchos.skills.literature_search import (
    PaperResult,
    ProviderOutcome,
    SearchOutcome,
    SearchStatus,
)
from researchos.skills.source_fetch import FetchOutcome, FetchStatus
from researchos.skills.web_search import WebSearchOutcome, WebSearchResult

COMPOUND = (
    "How do graph-based memory systems improve long-horizon retrieval, and what "
    "limitations remain compared with vector RAG?"
)
FOLLOW_UP = "Which limitations of graph-based memory systems remain compared with vector RAG?"

ENGINES = [ResearchWorkflow]
if maybe_langgraph_available():
    ENGINES.append(ResearchGraph)


class FakeAcademic:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls: List[tuple] = []
        self._lock = threading.Lock()

    def search_with_outcome(self, query, max_results=5, provider="both"):
        with self._lock:
            self.calls.append((query, provider))
        if provider in self.fail:
            return SearchOutcome(
                query=query, provider=provider,
                outcomes=[ProviderOutcome(provider=provider, status=SearchStatus.ERROR, error="upstream 503")],
                results=[], status=SearchStatus.ERROR,
            )
        papers = []
        if provider == "arxiv":
            papers = [
                PaperResult(
                    title="Graph-based memory systems improve long-horizon retrieval",
                    abstract=(
                        "Graph-based memory systems improve long-horizon retrieval by linking entities "
                        "across documents. Vector RAG limitations include losing relational structure "
                        "between retrieved chunks."
                    ),
                    arxiv_id="2501.00001", url="https://arxiv.org/abs/2501.00001", source="arxiv",
                ),
                PaperResult(
                    title="Memory graphs versus vector retrieval augmented generation",
                    abstract=(
                        "Graph memory limitations remain around construction cost and staleness "
                        "compared with vector RAG pipelines. Updating graph memory systems at scale "
                        "is an open problem."
                    ),
                    arxiv_id="2501.00002", url="https://arxiv.org/abs/2501.00002", source="arxiv",
                ),
            ]
        return SearchOutcome(
            query=query, provider=provider,
            outcomes=[ProviderOutcome(provider=provider, status=SearchStatus.OK, results=papers)],
            results=papers, status=SearchStatus.OK if papers else SearchStatus.EMPTY,
        )


class FakeWeb:
    enabled = True

    def __init__(self):
        self.calls = 0

    def search_with_outcome(self, query, max_results=None):
        self.calls += 1
        hit = WebSearchResult(title="Graph memory explainer", url="https://blog.example/graph-memory", snippet="s")
        return WebSearchOutcome(query=query, status=SearchStatus.OK, results=[hit])


class FakeFetch:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    def fetch(self, url):
        self.calls += 1
        if not self.ok:
            return FetchOutcome(url=url, status=FetchStatus.BLOCKED, error="disallowed by robots.txt", error_type="RobotsDisallowed")
        text = (
            "Graph memory systems improve long-horizon retrieval for agents by keeping entity relations. "
            "Limitations remain compared with vector RAG, mainly graph construction cost and staleness."
        )
        return FetchOutcome(url=url, final_url=url, status=FetchStatus.OK, title="Explainer", text=text)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "auto")
    monkeypatch.setenv("RESEARCHOS_LLM_CLAIM_EXTRACTION", "0")
    monkeypatch.delenv("RESEARCHOS_ORCHESTRATOR", raising=False)


def make(engine, ctx, academic=None, web=None, fetch=None, **kwargs):
    kwargs.setdefault("limits", ResearchLimits(max_workers=2, results_per_provider=2, max_research_s=20.0))
    return engine(ctx, literature_search=academic, web_search=web, source_fetch=fetch, **kwargs)


def run(workflow, query=COMPOUND, run_id="r1"):
    events: List[Dict[str, Any]] = []
    result = workflow.run(query, run_id=run_id, project_id="p", token_budget=1500, on_event=events.append)
    return result, events


def stage_sequence(events):
    seen: List[str] = []
    for event in events:
        if event["event"] == "stage" and event["stage"] not in seen:
            seen.append(event["stage"])
    return seen


@pytest.mark.parametrize("engine", ENGINES)
def test_compound_query_runs_plan_tasks_sources_and_citations(engine):
    ctx = CtxFlow()
    result, events = run(make(engine, ctx, FakeAcademic(), FakeWeb(), FakeFetch()))

    assert result["success"] is True
    assert stage_sequence(events) == list(STAGES)

    plan = result["research_plan"]
    assert plan["planner"] == "deterministic_fallback"
    assert 2 <= len(plan["subtasks"]) <= 4
    assert result["plan"] == list(STAGES)  # legacy stage list is preserved

    assert [t["status"] for t in result["tasks"]] == ["completed"] * len(plan["subtasks"])
    kinds = {e["kind"] for e in events if e["event"] == "task"}
    assert {"task_started", "task_completed"} <= kinds
    assert {e["provider"] for e in events if e["event"] == "task"} >= {"arxiv", "web", "fetch_source"}

    by_kind = {s["source_kind"]: s for s in result["sources"]}
    assert set(by_kind) == {"academic", "web"}
    assert by_kind["web"]["access_status"] == "full_text"
    assert by_kind["academic"]["access_status"] == "abstract_only"
    for source in result["sources"]:
        assert source["node_ids"], source  # every source resolved to graph nodes
        assert "text" not in source

    assert result["cited_nodes"]
    assert result["citations"]
    for citation in result["citations"]:
        assert ctx.graph.get_node(citation["node_id"]) is not None
        assert citation["source_url"] and citation["access_status"]
    assert result["verdict"] in {"VERIFIED", "WARNINGS_FOUND"}
    assert not [a for a in result["alerts"] if a["type"] == "missing_source"]


@pytest.mark.parametrize("engine", ENGINES)
def test_ingested_evidence_has_provenance_edges_and_metadata(engine):
    ctx = CtxFlow()
    result, _ = run(make(engine, ctx, FakeAcademic(), FakeWeb(), FakeFetch()))

    web_source = next(s for s in result["sources"] if s["source_kind"] == "web")
    paper_node = ctx.graph.get_node(web_source["node_ids"][0])
    assert paper_node.node_type == "paper"
    assert paper_node.metadata["access_status"] == "full_text"
    assert paper_node.metadata["source_url"] == "https://blog.example/graph-memory"
    assert paper_node.metadata["provider"] == "web"

    claim = ctx.graph.get_node(web_source["node_ids"][1])
    meta = claim.metadata
    assert meta["source_paper_node_id"] == paper_node.id
    assert meta["access_status"] == "full_text"
    assert meta["retrieved_at"] and meta["extraction_method"] == "sentence_overlap"
    assert isinstance(meta["span_start"], int) and isinstance(meta["span_end"], int)
    edges = ctx.graph.get_edges_by_type(claim.id, "extracted_from")
    assert any({e.source_id, e.target_id} == {claim.id, paper_node.id} for e in edges)

    abstract_claim = ctx.graph.get_node(
        next(s for s in result["sources"] if s["source_kind"] == "academic")["node_ids"][1]
    )
    assert abstract_claim.metadata["access_status"] == "abstract_only"
    assert abstract_claim.metadata["extraction_method"] == "heuristic_sentence"


@pytest.mark.parametrize("engine", ENGINES)
def test_follow_up_reuses_memory_without_searching_again(engine):
    ctx = CtxFlow()
    academic, web, fetch = FakeAcademic(), FakeWeb(), FakeFetch()
    first, _ = run(make(engine, ctx, academic, web, fetch), run_id="r1")
    calls_after_first = (len(academic.calls), web.calls, fetch.calls)
    nodes_after_first = ctx.graph.node_count
    assert first["external_search"]["status"] == "ok"

    second, events = run(make(engine, ctx, academic, web, fetch), query=FOLLOW_UP, run_id="r2")

    assert (len(academic.calls), web.calls, fetch.calls) == calls_after_first
    assert ctx.graph.node_count == nodes_after_first
    assert second["external_search"]["status"] == "skipped"
    assert {t["status"] for t in second["tasks"]} == {"skipped"}
    assert {e["kind"] for e in events if e["event"] == "task"} == {"task_skipped"}
    assert second["cited_nodes"]  # answered from previously ingested evidence


@pytest.mark.parametrize("engine", ENGINES)
def test_partial_provider_failure_is_degraded_but_keeps_valid_citations(engine):
    ctx = CtxFlow()
    result, events = run(make(engine, ctx, FakeAcademic(fail={"openalex"}), FakeWeb(), FakeFetch()))

    assert result["success"] is True
    assert result["status"] == "degraded" and result["degraded"] is True
    assert result["external_search"]["status"] == "partial"
    assert "openalex" in result["external_search"]["errors"]
    assert result["cited_nodes"] and result["citations"]
    assert any(e["kind"] == "task_failed" for e in events if e["event"] == "task")


@pytest.mark.parametrize("engine", ENGINES)
def test_all_sources_failing_gives_explicit_insufficient_evidence(engine):
    ctx = CtxFlow()
    academic = FakeAcademic(fail={"arxiv", "semantic_scholar", "openalex"})
    result, _ = run(make(engine, ctx, academic, None, None))

    assert result["external_search"]["status"] == "error"
    assert result["degraded"] is True and result["status"] == "degraded"
    assert result["cited_nodes"] == []
    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert result["answer"].startswith("Insufficient evidence")
    assert any("External search failed" in e for e in result["errors"])


@pytest.mark.parametrize("engine", ENGINES)
def test_failed_page_fetch_is_visible_and_never_ingested_as_evidence(engine):
    ctx = CtxFlow()
    result, _ = run(make(engine, ctx, None, FakeWeb(), FakeFetch(ok=False)))

    source = result["sources"][0]
    assert source["access_status"] == "failed"
    assert source["metadata"]["fetch_error"] == "disallowed by robots.txt"
    assert source["node_ids"] == []
    assert ctx.graph.node_count == 0
    assert result["answer"].startswith("Insufficient evidence")


@pytest.mark.parametrize("engine", ENGINES)
def test_external_search_off_never_calls_any_provider(engine, monkeypatch):
    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "off")
    academic, web = FakeAcademic(), FakeWeb()
    result, _ = run(make(engine, CtxFlow(), academic, web, FakeFetch()))

    assert academic.calls == [] and web.calls == 0
    assert result["external_search"]["status"] == "disabled"
    assert all(t["providers"] == [] for t in result["research_plan"]["subtasks"])


@pytest.mark.parametrize("engine", ENGINES)
def test_cancelled_run_stops_before_searching(engine):
    academic = FakeAcademic()
    cancel = threading.Event()
    cancel.set()
    workflow = make(engine, CtxFlow(), academic)
    result = workflow.run(COMPOUND, run_id="rc", project_id="p", cancel_event=cancel)

    assert academic.calls == []
    assert result["status"] == "cancelled"


def test_sequential_and_langgraph_engines_agree():
    if len(ENGINES) < 2:
        pytest.skip("langgraph not installed")
    outputs = []
    for engine in ENGINES:
        ctx = CtxFlow()
        result, _ = run(make(engine, ctx, FakeAcademic(), FakeWeb(), FakeFetch()))
        outputs.append(
            (
                [s["source_id"] for s in result["sources"]],
                [(t["task_id"], t["status"], t["result_count"]) for t in result["tasks"]],
                result["research_plan"],
            )
        )
    assert outputs[0] == outputs[1]


def test_reingesting_a_source_creates_no_duplicate_paper_node():
    ctx = CtxFlow()
    reader = ReaderAgent(ctx)
    paper = {
        "title": "Graph Memory Explainer", "url": "https://blog.example/a?utm_source=x",
        "source": "web", "source_kind": "web", "source_id": "url:https://blog.example/a",
        "access_status": "full_text", "retrieved_at": "2026-10-28T00:00:00+00:00",
        "claims": [{"type": "claim", "text": "Graph memory keeps entity relations across documents.",
                    "confidence": 0.5, "tags": ["graph"], "span_start": 0, "span_end": 54}],
    }
    first = reader.run(query="q", context={"previous_output": {"external_results": [paper]}})
    again = dict(paper, url="https://www.blog.example/a")
    second = reader.run(query="q", context={"previous_output": {"external_results": [again]}})

    papers = [n for n in ctx.graph.all_nodes() if n.node_type == "paper"]
    assert len(papers) == 1
    assert second.data["ingested_papers"][0]["reused"] is True
    assert second.data["total_nodes_created"] == 0
    assert first.data["ingested_papers"][0]["reused"] is False


def test_access_status_survives_save_and_reload_and_projects_are_isolated(tmp_path):
    from researchos.persistence import ResearchWorkspace

    workspace = ResearchWorkspace(data_dir=tmp_path)
    ctx_a = workspace.get_graph("project-a")
    workflow = make(
        ResearchWorkflow, ctx_a, FakeAcademic(), FakeWeb(), FakeFetch(),
        persist=lambda: workspace.save_graph("project-a"),
    )
    run(workflow)

    reloaded = ResearchWorkspace(data_dir=tmp_path)
    graph_a = reloaded.get_graph("project-a")
    statuses = {
        n.metadata.get("access_status") for n in graph_a.graph.all_nodes() if n.node_type == "paper"
    }
    assert statuses == {"abstract_only", "full_text"}
    assert all(
        n.metadata.get("source_url") for n in graph_a.graph.all_nodes() if n.node_type == "paper"
    )
    assert reloaded.get_graph("project-b").graph.node_count == 0
