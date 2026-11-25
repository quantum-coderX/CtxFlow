"""P1 gates: optional memory failures are visible, bounded and project-scoped."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, List

import pytest

from ctxflow import CtxFlow
from ctxflow.memory import InMemoryMemoryStore
from ctxflow.memory.store import Memory, MemoryType
from researchos.demo import load_corpus, seed_demo_corpus
from researchos.memory.context import MemoryContextProvider
from researchos.orchestration import workflow as workflow_module
from researchos.orchestration.workflow import (
    ResearchGraph,
    ResearchWorkflow,
    maybe_langgraph_available,
)

QUERY = "What does the research say about long-context retrieval?"
ENGINES = [ResearchWorkflow]
if maybe_langgraph_available():
    ENGINES.append(ResearchGraph)


@pytest.fixture()
def corpus(monkeypatch) -> CtxFlow:
    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "off")
    ctxflow = CtxFlow()
    seed_demo_corpus(ctxflow, project_id="demo", papers=load_corpus()[:3])
    return ctxflow


@pytest.fixture(params=[False, True], ids=["no-loop", "owned-loop"])
def memory_loop(request):
    if not request.param:
        yield None
        return
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)


class _Model:
    def __init__(self) -> None:
        self.prompts: List[str] = []

    def __call__(self, prompt, system=""):
        return "answer"

    def complete_json(self, prompt, system=""):
        self.prompts.append(prompt)
        return {"direct_answer": "Grounded answer.", "citation_ids": []}


class _FailingProvider:
    selector_name = "failing"

    async def retrieve(self, **_kwargs: Any):
        raise RuntimeError("store down")


class _SlowProvider:
    selector_name = "slow"

    async def retrieve(self, **_kwargs: Any):
        await asyncio.sleep(5)
        return []


class _EmptyProvider:
    selector_name = "empty"

    async def retrieve(self, **_kwargs: Any):
        return []


def _run(engine, corpus, provider, loop, run_id) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    result = engine(
        corpus, llm=_Model(), context_provider=provider, memory_loop=loop
    ).run(QUERY, run_id=run_id, project_id="demo", on_event=events.append)
    result["_events"] = events
    return result


def _memory_alerts(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [a for a in result["alerts"] if a.get("type") == "memory"]


@pytest.mark.parametrize("engine", ENGINES)
def test_store_exception_degrades_visibly(corpus, engine, memory_loop):
    result = _run(engine, corpus, _FailingProvider(), memory_loop, "mem-fail")

    assert result["success"] is True
    assert result["status"] == "degraded"
    assert result["degraded"] is True
    assert result["memory_context_ids"] == []
    assert _memory_alerts(result)
    memory_events = [e for e in result["_events"] if e["stage"] == "memory"]
    assert "degraded" in {e["status"] for e in memory_events}


@pytest.mark.parametrize("engine", ENGINES)
def test_retrieval_timeout_is_bounded_and_degrades(
    corpus, engine, memory_loop, monkeypatch
):
    monkeypatch.setattr(workflow_module, "_MEMORY_TIMEOUT_S", 0.05)
    result = _run(engine, corpus, _SlowProvider(), memory_loop, "mem-timeout")

    assert result["status"] == "degraded"
    assert _memory_alerts(result)
    assert result["memory_context_ids"] == []


@pytest.mark.parametrize("engine", ENGINES)
def test_empty_memory_is_not_degraded(corpus, engine, memory_loop):
    result = _run(engine, corpus, _EmptyProvider(), memory_loop, "mem-empty")

    assert result["status"] == "completed"
    assert result["memory_context_ids"] == []
    assert result["memory_selector"] == "empty"
    assert not _memory_alerts(result)


@pytest.mark.parametrize("engine", ENGINES)
def test_other_project_memory_never_reaches_run(corpus, engine, memory_loop):
    store = InMemoryMemoryStore()

    async def seed() -> None:
        await store.add(
            Memory(
                id="mem-demo",
                text="Long-context retrieval decision for the demo project.",
                memory_type=MemoryType.DECISION,
                project_id="demo",
            )
        )
        await store.add(
            Memory(
                id="mem-other",
                text="Long-context retrieval decision for the other project zebra.",
                memory_type=MemoryType.DECISION,
                project_id="other",
            )
        )

    asyncio.run(seed())
    model = _Model()
    result = engine(
        corpus,
        llm=model,
        context_provider=MemoryContextProvider(store),
        memory_loop=memory_loop,
    ).run(QUERY, run_id="mem-scope", project_id="demo")

    assert "mem-demo" in result["memory_context_ids"]
    assert "mem-other" not in result["memory_context_ids"]
    assert all("zebra" not in prompt for prompt in model.prompts)


@pytest.mark.parametrize("engine", ENGINES)
def test_selected_memory_never_exceeds_budget(corpus, engine, memory_loop):
    store = InMemoryMemoryStore()

    async def seed() -> None:
        for index in range(20):
            await store.add(
                Memory(
                    id=f"mem-{index}",
                    text=f"Long-context retrieval note {index} " + "detail " * 30,
                    memory_type=MemoryType.FACT,
                    project_id="demo",
                )
            )

    asyncio.run(seed())
    result = engine(
        corpus,
        llm=_Model(),
        context_provider=MemoryContextProvider(store),
        memory_loop=memory_loop,
    ).run(QUERY, run_id="mem-budget", project_id="demo", token_budget=400)

    used = sum(item["tokens"] for item in result["memory_context"])
    assert 0 < used <= int(400 * workflow_module._MEMORY_BUDGET_FRACTION)


def test_stream_api_reports_memory_fields_and_isolates_projects(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from researchos.api.server import create_app
    from researchos.persistence import ResearchWorkspace

    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "off")
    monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")
    monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
    monkeypatch.delenv("LAYA_CONTEXT_SELECTION", raising=False)

    store = InMemoryMemoryStore()

    async def seed() -> None:
        for memory_id, project, text in (
            ("mem-a", "proj-a", "Long-context retrieval decision for project alpha."),
            ("mem-b", "proj-b", "Long-context retrieval decision for project zebra."),
        ):
            await store.add(
                Memory(
                    id=memory_id,
                    text=text,
                    memory_type=MemoryType.DECISION,
                    project_id=project,
                )
            )

    asyncio.run(seed())
    workspace = ResearchWorkspace(data_dir=tmp_path)
    with TestClient(create_app(workspace=workspace, memory_store=store)) as client:
        client.post("/api/demo/seed", json={"project_id": "proj-a"})
        done = None
        current = None
        with client.stream(
            "GET", "/api/stream?query=long+context+retrieval&project_id=proj-a"
        ) as response:
            for line in response.iter_lines():
                if line.startswith("event:"):
                    current = line[6:].strip()
                elif line.startswith("data:") and current == "done":
                    done = json.loads(line[5:].strip())
                    break

    assert done is not None
    assert done["memory_selector"] == "greedy"
    assert done["memory_context_ids"] == ["mem-a"]
    assert "zebra" not in json.dumps(done["memory_context"])

