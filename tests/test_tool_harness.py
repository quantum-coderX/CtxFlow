"""Tests for bounded, dependency-aware tool execution."""

from __future__ import annotations

import asyncio

import pytest

from researchos.harness import HarnessPlanError, ToolHarness, ToolTask
from researchos.skills.registry import SkillRegistry


@pytest.mark.asyncio
async def test_independent_tasks_run_concurrently_and_keep_plan_order():
    registry = SkillRegistry()
    started = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_peer(value):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        await release.wait()
        return value

    registry.register_function("graph_query", wait_for_peer)
    harness = ToolHarness(registry, max_parallel=2)
    tasks = [
        ToolTask("first", "graph_query", {"value": "a"}),
        ToolTask("second", "graph_query", {"value": "b"}),
    ]

    execution = asyncio.create_task(harness.run(tasks))
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    result = await execution

    assert result.success
    assert [task.task_id for task in result.tasks] == ["first", "second"]
    assert [task.data for task in result.tasks] == ["a", "b"]


@pytest.mark.asyncio
async def test_dependent_task_receives_only_declared_output_reference():
    registry = SkillRegistry()
    registry.register_function(
        "literature_search",
        lambda query: {"papers": [{"title": query}]},
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    registry.register_function(
        "paper_reader",
        lambda title: {"read": title},
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )
    harness = ToolHarness(registry)

    result = await harness.run(
        [
            ToolTask("search", "literature_search", {"query": "paper A"}),
            ToolTask(
                "read",
                "paper_reader",
                {"title": {"$ref": "search.papers.0.title"}},
                depends_on=("search",),
            ),
        ]
    )

    assert result.success
    assert result.tasks[1].data == {"read": "paper A"}


@pytest.mark.asyncio
async def test_failed_task_blocks_dependents_but_unrelated_task_runs():
    registry = SkillRegistry()

    def fail(query):
        del query
        raise RuntimeError("provider unavailable")

    registry.register_function("literature_search", fail)
    registry.register_function("graph_query", lambda: "independent")
    registry.register_function("paper_reader", lambda: "should not run")
    harness = ToolHarness(registry)

    result = await harness.run(
        [
            ToolTask("search", "literature_search", {"query": "paper"}),
            ToolTask("read", "paper_reader", depends_on=("search",)),
            ToolTask("graph", "graph_query"),
        ]
    )

    assert result.status == "partial"
    assert [task.status for task in result.tasks] == [
        "failed",
        "blocked",
        "completed",
    ]
    assert "provider unavailable" in result.tasks[0].error
    assert "search" in result.tasks[1].error
    assert result.tasks[2].data == "independent"


@pytest.mark.asyncio
async def test_rejects_cycles_and_undeclared_output_references_before_execution():
    registry = SkillRegistry()
    calls = []
    registry.register_function("graph_query", lambda: calls.append("called"))
    harness = ToolHarness(registry)

    with pytest.raises(HarnessPlanError, match="cycle"):
        await harness.run(
            [
                ToolTask("a", "graph_query", depends_on=("b",)),
                ToolTask("b", "graph_query", depends_on=("a",)),
            ]
        )

    with pytest.raises(HarnessPlanError, match="without declaring"):
        await harness.run(
            [
                ToolTask("a", "graph_query"),
                ToolTask("b", "graph_query", {"query": {"$ref": "a.value"}}),
            ]
        )
    assert calls == []


@pytest.mark.asyncio
async def test_does_not_run_tools_outside_the_allowlist():
    registry = SkillRegistry()
    calls = []
    registry.register_function("shell", lambda: calls.append("called"))

    result = await ToolHarness(registry).run([ToolTask("unsafe", "shell")])

    assert result.success is False
    assert result.tasks[0].status == "failed"
    assert "allowlist" in result.tasks[0].error
    assert calls == []
