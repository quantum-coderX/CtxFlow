"""Planner, source normalization and bounded parallel executor (offline)."""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, List

import pytest

from researchos.orchestration.research_tasks import (
    PlanValidationError,
    ResearchLimits,
    ResearchTaskExecutor,
    SourceRecord,
    extract_excerpts,
    merge_records,
    plan_query,
    record_from_paper,
    split_question,
    validate_plan_tasks,
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
PROVIDERS = ("arxiv", "semantic_scholar", "openalex", "web")


# -- planner -----------------------------------------------------------------

def test_compound_query_splits_into_distinct_subtasks():
    plan = plan_query(COMPOUND, providers=PROVIDERS)

    assert plan.planner == "deterministic_fallback"
    assert 2 <= len(plan.subtasks) <= 4
    assert len({t.query for t in plan.subtasks}) == len(plan.subtasks)
    assert [t.task_id for t in plan.subtasks] == ["task-01", "task-02"]
    assert all(t.providers == PROVIDERS for t in plan.subtasks)
    assert plan.primary_question == COMPOUND


def test_simple_query_stays_single_task():
    plan = plan_query("long-context retrieval", providers=PROVIDERS)
    assert len(plan.subtasks) == 1
    assert split_question("What does the research say about long-context retrieval?") == []


class JsonModel:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def complete_json(self, prompt, system=""):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_model_plan_is_used_when_valid():
    model = JsonModel(
        {"subtasks": [
            {"query": "graph memory long horizon retrieval", "purpose": "mechanisms"},
            {"query": "graph memory vs vector RAG limitations", "purpose": "limits",
             "providers": ["arxiv"]},
        ]}
    )
    plan = plan_query(COMPOUND, providers=PROVIDERS, llm=model)

    assert plan.planner == "model"
    assert [t.query for t in plan.subtasks][0] == "graph memory long horizon retrieval"
    # providers always come from application policy, not the model
    assert all(t.providers == PROVIDERS for t in plan.subtasks)


@pytest.mark.parametrize(
    "payload",
    [
        RuntimeError("provider down"),
        "not json",
        {"subtasks": "nope"},
        {"subtasks": [{"query": "only one"}]},
        {"subtasks": [{"query": ""}, {"query": "x"}]},
        {"subtasks": [{"query": "a b"}, {"query": "A  B!"}]},
        {"subtasks": [{"query": f"q{i}"} for i in range(5)]},
        {"subtasks": [{"query": "a", "providers": ["shell"]}, {"query": "b"}]},
        {"subtasks": [{"query": "x" * 400}, {"query": "y"}]},
    ],
)
def test_invalid_model_plans_fall_back_deterministically(payload):
    plan = plan_query(COMPOUND, providers=PROVIDERS, llm=JsonModel(payload))
    assert plan.planner == "deterministic_fallback"
    assert 2 <= len(plan.subtasks) <= 4


def test_model_is_not_called_for_simple_queries():
    model = JsonModel({"subtasks": []})
    plan_query("long-context retrieval", providers=PROVIDERS, llm=model)
    assert model.calls == 0


def test_validate_rejects_unsafe_policy_providers():
    with pytest.raises(PlanValidationError):
        validate_plan_tasks([{"query": "a"}], providers=("shell",))
    with pytest.raises(PlanValidationError):
        validate_plan_tasks([{"query": "a"}, {"query": "a"}], providers=PROVIDERS)


# -- sources -----------------------------------------------------------------

def paper(title="A Paper", **kw) -> PaperResult:
    return PaperResult(title=title, abstract=kw.pop("abstract", "An abstract."), **kw)


def test_dedupe_prefers_identifiers_then_urls_then_titles():
    a = record_from_paper(paper("Graph Memory Systems Survey", arxiv_id="2401.00001v2", source="arxiv"), "arxiv", "task-01")
    b = record_from_paper(paper("Different title entirely here", arxiv_id="2401.00001", doi="10.1/abc", source="openalex"), "openalex", "task-02")
    c = record_from_paper(paper("Another one", doi="https://doi.org/10.1/ABC", source="semantic_scholar"), "semantic_scholar", "task-02")
    d = record_from_paper(paper("Distinct", url="https://www.site.org/p/?utm_source=a"), "web", "task-01")
    e = record_from_paper(paper("Distinct copy", url="http://site.org/p"), "web", "task-02")
    short1 = record_from_paper(paper("Short"), "arxiv", "task-01")
    short2 = record_from_paper(paper("Short"), "openalex", "task-02")

    merged = merge_records([a, b, c, d, e, short1, short2])

    assert len(merged) == 4  # {a,b,c}, {d,e}, short1, short2 (short titles never merge)
    first = merged[0]
    assert first.task_ids == ["task-01", "task-02"]
    assert first.metadata["providers"] == ["arxiv", "openalex", "semantic_scholar"]
    assert first.source_id == "doi:10.1/abc"  # DOI wins once known


def test_excerpts_are_verbatim_and_span_indexed():
    text = (
        "Intro paragraph without relevance to anything at all today.\n"
        "Graph-based memory systems improve long-horizon retrieval by linking entities across documents.\n"
        "Unrelated sentence about cooking pasta in a large pot of water.\n"
        "Vector RAG limitations include losing relational structure between retrieved chunks of text."
    )
    excerpts = extract_excerpts(text, "graph memory long-horizon retrieval vector RAG limitations")

    assert 1 <= len(excerpts) <= 3
    for item in excerpts:
        assert text[item["span_start"]:item["span_end"]] == item["text"]
        assert item["extraction_method"] == "sentence_overlap"
    assert not any("pasta" in item["text"] for item in excerpts)
    assert [e["span_start"] for e in excerpts] == sorted(e["span_start"] for e in excerpts)


# -- executor ----------------------------------------------------------------

class FakeAcademic:
    """Provider-aware fake that records overlap and honours shuffled latency."""

    def __init__(self, delays=None, fail=(), per_provider=1):
        self.delays = delays or {}
        self.fail = set(fail)
        self.per_provider = per_provider
        self.active = 0
        self.peak = 0
        self.calls: List[tuple] = []
        self._lock = threading.Lock()

    def search_with_outcome(self, query, max_results=5, provider="both"):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((query, provider))
        try:
            time.sleep(self.delays.get(query, 0.0))
            if (query, provider) in self.fail or provider in self.fail:
                raise RuntimeError(f"{provider} exploded")
            papers = [
                PaperResult(
                    title=f"{provider} result {i} for {query}",
                    abstract=f"Abstract {i} about {query}.",
                    arxiv_id=f"{abs(hash((query, provider, i))) % 10**6:06d}",
                    source=provider,
                )
                for i in range(self.per_provider)
            ]
            return SearchOutcome(
                query=query, provider=provider,
                outcomes=[ProviderOutcome(provider=provider, status=SearchStatus.OK, results=papers)],
                results=papers, status=SearchStatus.OK,
            )
        finally:
            with self._lock:
                self.active -= 1


class FakeWeb:
    enabled = True

    def __init__(self, urls=("https://site.example/a",)):
        self.urls = urls

    def search_with_outcome(self, query, max_results=None):
        hits = [WebSearchResult(title=f"Web {u}", url=u, snippet="snippet only") for u in self.urls]
        return WebSearchOutcome(query=query, status=SearchStatus.OK, results=hits)


class FakeFetch:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def fetch(self, url):
        self.calls.append(url)
        if not self.ok:
            return FetchOutcome(url=url, status=FetchStatus.ERROR, error="HTTP 503", error_type="HTTPStatus")
        text = (
            "Graph memory improves long-horizon retrieval by preserving entity relations across documents. "
            "Vector RAG limitations include weaker multi-hop reasoning over scattered chunks."
        )
        return FetchOutcome(url=url, final_url=url, status=FetchStatus.OK, title="Page", text=text)


def make_executor(academic=None, web=None, fetch=None, **limits):
    base = dict(max_workers=2, results_per_provider=1, max_research_s=30.0)
    base.update(limits)
    return ResearchTaskExecutor(
        run_id="run-1", literature_search=academic, web_search=web,
        source_fetch=fetch, limits=ResearchLimits(**base),
    )


def run_plan(executor, question=COMPOUND, **kwargs):
    plan = plan_query(question, providers=executor.available_providers())
    return plan, executor.execute(plan, **kwargs)


def test_workers_overlap_but_never_exceed_the_cap():
    academic = FakeAcademic()
    executor = make_executor(academic)
    plan = plan_query(
        "alpha study of things, and what beta results exist? Also how does gamma work; how does delta behave?",
        providers=executor.available_providers(),
    )
    assert len(plan.subtasks) == 4
    academic.delays = {t.query: 0.15 for t in plan.subtasks}

    result = executor.execute(plan)

    assert academic.peak == 2  # overlapped, capped at max_workers
    assert [t["status"] for t in result.tasks] == ["completed"] * 4


def test_output_order_is_stable_under_shuffled_completion():
    outputs = []
    for seed in range(4):
        rng = random.Random(seed)
        academic = FakeAcademic()
        executor = make_executor(academic, max_workers=3)
        plan = plan_query(
            "alpha study of things, and what beta results exist? Also how does gamma work",
            providers=executor.available_providers(),
        )
        academic.delays = {t.query: rng.random() * 0.1 for t in plan.subtasks}
        result = executor.execute(plan)
        outputs.append(
            (
                [s.source_id for s in result.sources],
                [(e["task_id"], e["kind"], e["provider"]) for e in result.events],
            )
        )
    assert all(item == outputs[0] for item in outputs)
    assert len(outputs[0][0]) >= 6


def test_failed_provider_does_not_hide_siblings():
    executor = make_executor(FakeAcademic(fail={"openalex"}))
    _, result = run_plan(executor)

    assert result.external_search["status"] == "partial"
    assert result.degraded is True
    assert result.sources
    assert "openalex" in result.external_search["errors"]
    assert {t["status"] for t in result.tasks} == {"partial"}
    kinds = {e["kind"] for e in result.events}
    assert {"task_started", "task_completed", "task_failed"} <= kinds


def test_all_providers_failing_is_an_error_not_empty():
    executor = make_executor(FakeAcademic(fail={"arxiv", "semantic_scholar", "openalex"}))
    _, result = run_plan(executor)

    assert result.external_search["status"] == "error"
    assert result.sources == []
    assert result.errors and "External search failed" in result.errors[0]
    assert {t["status"] for t in result.tasks} == {"failed"}


def test_cancellation_stops_new_work():
    cancel = threading.Event()
    cancel.set()
    academic = FakeAcademic()
    _, result = run_plan(make_executor(academic), cancel_event=cancel)

    assert academic.calls == []
    assert {t["status"] for t in result.tasks} == {"skipped"}
    assert {e["kind"] for e in result.events} == {"task_skipped"}


def test_wall_time_cap_stops_waiting_and_marks_unfinished_tasks():
    academic = FakeAcademic()
    executor = make_executor(academic, max_workers=1, max_research_s=0.3)
    plan = plan_query(COMPOUND, providers=executor.available_providers())
    academic.delays = {t.query: 0.5 for t in plan.subtasks}

    started = time.monotonic()
    result = executor.execute(plan)

    assert time.monotonic() - started < 1.5
    statuses = [t["status"] for t in result.tasks]
    assert "completed" not in statuses
    assert set(statuses) <= {"failed", "skipped"}


def test_web_sources_are_fetched_within_budget_and_keep_access_status():
    web, fetch = FakeWeb(urls=tuple(f"https://site.example/{i}" for i in range(6))), FakeFetch()
    executor = make_executor(None, web, fetch, max_fetched_pages=2, results_per_provider=6)
    _, result = run_plan(executor)

    assert len(fetch.calls) == 2
    statuses = [s.access_status for s in result.sources]
    assert statuses.count("full_text") == 2
    assert all(s.excerpts for s in result.sources if s.access_status == "full_text")
    skipped = [s for s in result.sources if s.metadata.get("fetch_status") == "skipped"]
    assert skipped and all(s.access_status == "unavailable" for s in skipped)
    assert {e["provider"] for e in result.events} >= {"web", "fetch_source"}


def test_failed_fetch_is_marked_failed_and_never_full_text():
    executor = make_executor(None, FakeWeb(), FakeFetch(ok=False))
    _, result = run_plan(executor)

    source = result.sources[0]
    assert source.access_status == "failed"
    assert source.text == "" and source.excerpts == []
    assert source.metadata["fetch_error"] == "HTTP 503"
    assert "snippet only" == source.metadata["snippet"]


def test_disabled_web_provider_is_not_offered():
    web = FakeWeb()
    web.enabled = False
    executor = make_executor(FakeAcademic(), web)
    assert "web" not in executor.available_providers()


def test_public_source_record_never_contains_page_text():
    executor = make_executor(None, FakeWeb(), FakeFetch())
    _, result = run_plan(executor)

    public = result.sources[0].to_public_dict()
    assert public["text_chars"] > 0 and "text" not in public
    assert public["access_status"] == "full_text"
