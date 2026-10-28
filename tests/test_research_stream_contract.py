"""SSE and run-history contract for plan, task progress, sources and citations."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import researchos.skills.literature_search as ls  # noqa: E402
from researchos.api.server import create_app  # noqa: E402
from researchos.persistence import ResearchWorkspace  # noqa: E402
from researchos.skills.literature_search import PaperResult, ProviderOutcome, SearchStatus  # noqa: E402
from researchos.skills.source_fetch import FetchOutcome, FetchStatus, SourceFetchSkill  # noqa: E402
from researchos.skills.web_search import DDGSProvider  # noqa: E402

QUERY = (
    "How do graph-based memory systems improve long-horizon retrieval, and what "
    "limitations remain compared with vector RAG?"
)
FOLLOW_UP = "Which limitations of graph-based memory systems remain compared with vector RAG?"


def _arxiv(*args, **kwargs):
    papers = [
        PaperResult(
            title="Graph-based memory systems improve long-horizon retrieval",
            abstract=(
                "Graph-based memory systems improve long-horizon retrieval by linking entities. "
                "Vector RAG limitations include losing relational structure between chunks."
            ),
            arxiv_id="2501.00001", url="https://arxiv.org/abs/2501.00001", source="arxiv",
        ),
        PaperResult(
            title="Memory graphs versus vector retrieval augmented generation",
            abstract=(
                "Graph memory limitations remain around construction cost and staleness compared "
                "with vector RAG. Updating graph memory at scale is an open problem."
            ),
            arxiv_id="2501.00002", url="https://arxiv.org/abs/2501.00002", source="arxiv",
        ),
    ]
    return ProviderOutcome(provider="arxiv", status=SearchStatus.OK, results=papers)


def _empty(provider):
    return lambda *args, **kwargs: ProviderOutcome(provider=provider, status=SearchStatus.EMPTY)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the literature cache lives in ./.cache
    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "auto")
    monkeypatch.setenv("RESEARCHOS_WEB_SEARCH", "on")
    monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")
    monkeypatch.setenv("RESEARCHOS_LLM_CLAIM_EXTRACTION", "0")
    monkeypatch.setattr(ls, "search_arxiv_outcome", _arxiv)
    monkeypatch.setattr(ls, "search_semantic_scholar_outcome", _empty("semantic_scholar"))
    monkeypatch.setattr(ls, "search_openalex_outcome", _empty("openalex"))
    monkeypatch.setattr(
        DDGSProvider,
        "search",
        lambda self, query, max_results: [
            {"title": "Graph memory explainer", "url": "https://blog.example/graph", "snippet": "s"}
        ],
    )
    monkeypatch.setattr(
        SourceFetchSkill,
        "fetch",
        lambda self, url: FetchOutcome(
            url=url, final_url=url, status=FetchStatus.OK, title="Explainer",
            text=(
                "Graph memory systems improve long-horizon retrieval for agents by keeping entity "
                "relations. Limitations remain compared with vector RAG, mainly construction cost."
            ),
        ),
    )
    with TestClient(create_app(workspace=ResearchWorkspace(data_dir=tmp_path))) as test_client:
        yield test_client


def _stream(client: TestClient, query: str, project: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    current = None
    with client.stream("GET", "/api/stream", params={"query": query, "project_id": project}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event:"):
                current = line[6:].strip()
            elif line.startswith("data:"):
                items.append({"event": current, "data": json.loads(line[5:].strip())})
                if current == "done":
                    break
    return items


def test_stream_emits_plan_task_stage_and_done_payloads(client):
    items = _stream(client, QUERY, "stream-plan")
    names = [item["event"] for item in items]

    assert names[0] == "start" and names[-1] == "done"
    plan_event = next(i for i in items if i["event"] == "plan" and i["data"]["status"] == "completed")
    assert 2 <= len(plan_event["data"]["data"]["research_plan"]["subtasks"]) <= 4

    task_events = [i["data"] for i in items if i["event"] == "task"]
    assert {e["kind"] for e in task_events} >= {"task_started", "task_completed"}
    assert {e["provider"] for e in task_events} >= {"arxiv", "web", "fetch_source"}
    assert all({"task_id", "provider", "status", "data"} <= set(e) for e in task_events)
    # task events arrive inside the retrieve stage, before it completes
    retrieve_done = next(
        n for n, i in enumerate(items) if i["event"] == "retrieve" and i["data"]["status"] == "completed"
    )
    assert max(n for n, i in enumerate(items) if i["event"] == "task") < retrieve_done

    done = items[-1]["data"]
    assert done["success"] is True
    assert done["research_plan"]["planner"] == "deterministic_fallback"
    assert {s["source_kind"] for s in done["sources"]} == {"academic", "web"}
    assert done["citations"] and all(c["source_url"] and c["access_status"] for c in done["citations"])
    assert "api_key" not in json.dumps(items)
    assert not any("text" in s for s in done["sources"])


def test_run_history_keeps_plan_tasks_and_sources(client):
    _stream(client, QUERY, "history")
    runs = client.get("/api/runs", params={"project_id": "history"}).json()["runs"]

    latest = runs[0]
    assert latest["research_plan"]["subtasks"]
    assert latest["tasks"] and latest["sources"] and latest["citations"]
    assert latest["plan"][0] == "plan"  # legacy stage list unchanged
    kinds = {e.get("kind") for e in latest["events"] if e.get("event") == "task"}
    assert "task_completed" in kinds

    by_id = client.get(f"/api/runs/{latest['run_id']}").json()
    assert by_id["sources"] == latest["sources"]


def test_follow_up_streams_only_skipped_tasks(client):
    _stream(client, QUERY, "reuse")
    items = _stream(client, FOLLOW_UP, "reuse")

    kinds = {i["data"]["kind"] for i in items if i["event"] == "task"}
    assert kinds == {"task_skipped"}
    done = items[-1]["data"]
    assert done["external_search"]["status"] == "skipped"
    assert done["cited_nodes"]
