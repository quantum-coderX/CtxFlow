"""Laya tier proposals and cache warming for paged memory."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ctxflow.laya_selector import HeuristicLayaRouter
from ctxflow.laya_tiering import LayaTierAdvisor
from ctxflow.memory import (
    InMemoryMemoryStore,
    Memory,
    MemoryTier,
    PagedMemoryGraph,
    StorageTier,
    ValidityState,
)
from ctxflow.memory.store import _now
from researchos.api.server import create_app
from researchos.persistence import ResearchWorkspace


def _mem(memory_id: str, **kwargs: Any) -> Memory:
    kwargs.setdefault("created_at", _now() - timedelta(days=30))
    return Memory(text=f"text {memory_id}", id=memory_id, **kwargs)


class _Router:
    name = "fake"

    def __init__(self, validity="valid", importance=1, confidence=0.9, error=False):
        self.answers = {
            "validity_check": {"answer": validity, "confidence": confidence},
            "importance": {"answer": importance, "confidence": confidence},
        }
        self.error = error
        self.calls = 0

    def predict(self, state, questions, model="english", max_len=1024):
        self.calls += 1
        if self.error:
            raise RuntimeError("boom")
        return self.answers


def _propose(router, memory):
    return LayaTierAdvisor(router).propose([memory])[0]


def test_rule_cold_is_never_moved_and_skips_the_router():
    router = _Router(importance=2)
    proposal = _propose(router, _mem("a", validity=ValidityState.SUPERSEDED))
    assert proposal.proposed_tier is StorageTier.COLD
    assert proposal.reason == "rule_cold"
    assert router.calls == 0


def test_important_warm_memory_is_promoted_to_hot():
    proposal = _propose(_Router(importance=2), _mem("a"))
    assert (proposal.rule_tier, proposal.proposed_tier) == (
        StorageTier.WARM,
        StorageTier.HOT,
    )
    assert proposal.reason == "laya_important"


def test_invalid_and_low_value_are_demoted():
    assert _propose(_Router(validity="invalid"), _mem("a")).proposed_tier is StorageTier.COLD
    low = _propose(_Router(validity="uncertain", importance=0), _mem("a"))
    assert (low.proposed_tier, low.reason) == (StorageTier.COLD, "laya_low_value")


def test_working_memory_is_never_demoted():
    proposal = _propose(_Router(validity="invalid"), _mem("a", tier=MemoryTier.WORKING))
    assert proposal.rule_tier is StorageTier.HOT
    assert proposal.proposed_tier is StorageTier.HOT


def test_low_confidence_and_errors_fall_back_to_rules():
    weak = _propose(_Router(validity="invalid", confidence=0.2), _mem("a"))
    assert (weak.proposed_tier, weak.reason) == (StorageTier.WARM, "low_confidence")
    failed = _propose(_Router(error=True), _mem("a"))
    assert (failed.proposed_tier, failed.reason) == (StorageTier.WARM, "laya_error")


def test_default_router_proposes_without_the_laya_package():
    advisor = LayaTierAdvisor(HeuristicLayaRouter())
    important = advisor.propose([_mem("a", importance=0.9)])[0]
    assert important.proposed_tier is StorageTier.HOT
    assert important.router == "heuristic"


def test_invalid_min_confidence_is_rejected():
    with pytest.raises(ValueError):
        LayaTierAdvisor(_Router(), min_confidence=1.5)


def test_warm_preloads_only_requested_pages():
    async def run():
        store = InMemoryMemoryStore()
        for index in range(9):
            await store.add(_mem(f"m{index}", importance=0.9))
        graph = PagedMemoryGraph(store, "default", page_size=2, ttl_s=None)
        loaded = await graph.warm(tier=StorageTier.HOT, max_pages=3)
        return loaded, graph.cached_pages, graph.stats.misses

    assert asyncio.run(run()) == (3, 3, 3)


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
    monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    store = InMemoryMemoryStore()
    asyncio.run(store.add(_mem("m1", importance=0.9)))
    workspace = ResearchWorkspace(data_dir=tmp_path)
    return TestClient(create_app(workspace=workspace, memory_store=store))


def test_advise_requires_the_tiering_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("LAYA_TIERING", raising=False)
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/memory/page", params={"advise": "true"})
    assert response.status_code == 400


def test_advise_returns_proposals_when_enabled(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, LAYA_TIERING="true") as client:
        plain = client.get("/api/memory/page").json()
        advised = client.get("/api/memory/page", params={"advise": "true"}).json()

    assert plain["memories"][0]["proposed_tier"] is None
    item = advised["memories"][0]
    assert item["proposed_tier"] == "hot"
    assert item["tier_router"] in {"heuristic", "laya"}
    assert item["tier_reason"]
