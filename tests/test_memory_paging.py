"""Paged memory reads: tiers, keyset pages, edge scoping, LRU cache, API."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ctxflow.memory import (
    Edge,
    InMemoryMemoryStore,
    Memory,
    MemoryTier,
    MemoryType,
    PagedMemoryGraph,
    PostgresMemoryStore,
    Relation,
    StorageTier,
    ValidityState,
    storage_tier,
)
from ctxflow.memory.store import _now
from researchos.api.server import create_app
from researchos.persistence import ResearchWorkspace


def _memory(memory_id: str, project: str = "p", **kwargs) -> Memory:
    return Memory(text=f"text {memory_id}", id=memory_id, project_id=project, **kwargs)


def _old(**kwargs) -> dict:
    return {"created_at": _now() - timedelta(days=30), **kwargs}


async def _seeded() -> InMemoryMemoryStore:
    store = InMemoryMemoryStore()
    await store.add(_memory("m01"))  # recent -> hot
    await store.add(_memory("m02", **_old()))  # warm
    await store.add(_memory("m03", **_old(importance=0.9)))  # hot by importance
    await store.add(_memory("m04", **_old(validity=ValidityState.SUPERSEDED)))  # cold
    await store.add(_memory("m05", **_old(archived=True)))  # cold
    await store.add(_memory("m06", **_old(tier=MemoryTier.WORKING)))  # hot
    await store.add(_memory("x01", project="other"))
    return store


def test_storage_tier_rules():
    now = _now()
    assert storage_tier(_memory("a", **_old()), now=now) is StorageTier.WARM
    assert storage_tier(_memory("a"), now=now) is StorageTier.HOT
    assert storage_tier(_memory("a", **_old(importance=0.8)), now=now) is StorageTier.HOT
    assert storage_tier(_memory("a", **_old(validity=ValidityState.STALE)), now=now) is StorageTier.COLD
    # Cold wins over recency and importance.
    assert (
        storage_tier(_memory("a", importance=1.0, archived=True), now=now)
        is StorageTier.COLD
    )


def test_list_page_walks_project_once_and_stays_scoped():
    async def run():
        store = await _seeded()
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = await store.list_page(project_id="p", cursor=cursor, limit=4)
            assert len(page.memories) <= 4
            seen.extend(m.id for m in page.memories)
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        return seen, pages

    seen, pages = asyncio.run(run())
    assert seen == ["m01", "m02", "m03", "m04", "m05", "m06"]
    assert pages == 2  # no trailing empty page


def test_list_page_filters_by_tier_and_type():
    async def run():
        store = await _seeded()
        out = {}
        for tier in StorageTier:
            page = await store.list_page(project_id="p", tier=tier)
            out[tier] = [m.id for m in page.memories]
        typed = await store.list_page(project_id="p", types=[MemoryType.FACT])
        return out, typed

    out, typed = asyncio.run(run())
    assert out[StorageTier.HOT] == ["m01", "m03", "m06"]
    assert out[StorageTier.WARM] == ["m02"]
    assert out[StorageTier.COLD] == ["m04", "m05"]
    assert typed.memories == []  # default type is conversation


def test_list_page_rejects_bad_limit():
    with pytest.raises(ValueError):
        asyncio.run(InMemoryMemoryStore().list_page(limit=0))


def test_edges_for_is_project_scoped_and_directional():
    async def run():
        store = await _seeded()
        await store.add_edge(Edge("m01", "m02", Relation.RELATED_TO))
        await store.add_edge(Edge("m01", "m02", Relation.DEPENDS_ON))
        await store.add_edge(Edge("m03", "m01", Relation.DERIVED_FROM))
        return (
            await store.edges_for(["m01"], project_id="p"),
            await store.edges_for(["m01"], project_id="p", direction="in"),
            await store.edges_for(["m01"], project_id="p", direction="both"),
            await store.edges_for(["m01"], project_id="other"),
        )

    out, incoming, both, other = asyncio.run(run())
    assert {e.relation for e in out} == {Relation.RELATED_TO, Relation.DEPENDS_ON}
    assert [e.src for e in incoming] == ["m03"]
    assert len(both) == 3
    assert other == []


def test_page_cache_hits_evicts_and_invalidates():
    async def run():
        store = await _seeded()
        graph = PagedMemoryGraph(store, "p", page_size=2, max_pages=2, ttl_s=None)
        await graph.page()
        await graph.page()
        assert (graph.stats.hits, graph.stats.misses) == (1, 1)

        first = await graph.page()
        await graph.page(first.next_cursor)
        await graph.page("m04")  # third distinct key
        assert graph.cached_pages == 2
        assert graph.stats.evictions == 1

        graph.invalidate()
        assert graph.cached_pages == 0

        ids = [m.id async for page in graph.iterate() for m in page.memories]
        return ids

    assert asyncio.run(run()) == ["m01", "m02", "m03", "m04", "m05", "m06"]


def test_page_cache_ttl_expires_entries():
    async def run():
        graph = PagedMemoryGraph(await _seeded(), "p", ttl_s=-1.0)
        await graph.page()
        await graph.page()
        return graph.stats

    stats = asyncio.run(run())
    assert (stats.hits, stats.misses) == (0, 2)


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


class _FakePool:
    def __init__(self, rows):
        self.connection = _FakeConnection(rows)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.connection

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _row(memory_id: str) -> dict:
    return {
        "id": memory_id, "text": "t", "memory_type": "fact", "tier": "episodic",
        "created_at": _now(), "session_id": None, "project_id": "p", "source": "",
        "importance": 0.1, "access_count": 0, "current_version": 1,
        "validity": "valid", "archived": False, "entities": [],
        "embedding_model": "", "embedding": None, "token_counts": {},
    }


def test_postgres_list_page_requests_limit_plus_one_and_sets_cursor():
    pool = _FakePool([_row("a"), _row("b"), _row("c")])
    store = PostgresMemoryStore(pool)

    page = asyncio.run(
        store.list_page(project_id="p", cursor="0", limit=2, tier=StorageTier.COLD)
    )

    sql, args = pool.connection.calls[0]
    assert args[0] == "p" and args[1] == "0"
    assert args[3] == "cold" and args[-1] == 3
    assert "ORDER BY id" in sql and "project_id=$1" in sql
    assert [m.id for m in page.memories] == ["a", "b"]
    assert page.next_cursor == "b"


def test_postgres_edges_for_skips_query_when_empty():
    pool = _FakePool([])
    assert asyncio.run(PostgresMemoryStore(pool).edges_for([])) == []
    assert pool.connection.calls == []


def test_memory_page_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
    monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")
    store = asyncio.run(_seeded())
    workspace = ResearchWorkspace(data_dir=tmp_path)

    with TestClient(create_app(workspace=workspace, memory_store=store)) as client:
        first = client.get("/api/memory/page", params={"project_id": "p", "limit": 4}).json()
        second = client.get(
            "/api/memory/page",
            params={"project_id": "p", "limit": 4, "cursor": first["next_cursor"]},
        ).json()
        cold = client.get("/api/memory/page", params={"project_id": "p", "tier": "cold"}).json()
        bad = client.get("/api/memory/page", params={"project_id": "p", "tier": "lukewarm"})

    assert [m["id"] for m in first["memories"]] == ["m01", "m02", "m03", "m04"]
    assert [m["id"] for m in second["memories"]] == ["m05", "m06"]
    assert second["next_cursor"] is None
    assert [m["id"] for m in cold["memories"]] == ["m04", "m05"]
    assert all(m["storage_tier"] == "cold" for m in cold["memories"])
    assert bad.status_code == 422


def test_memory_page_endpoint_unavailable_without_memory(tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCHOS_MEMORY_ENABLED", raising=False)
    monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")
    workspace = ResearchWorkspace(data_dir=tmp_path)

    with TestClient(create_app(workspace=workspace)) as client:
        response = client.get("/api/memory/page", params={"project_id": "p"})

    assert response.status_code == 503
