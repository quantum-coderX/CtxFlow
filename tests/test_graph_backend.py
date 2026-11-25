"""Project graphs on a pluggable backend (PostgreSQL store, workspace wiring)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import pytest

from ctxflow.graph import ContextGraph
from ctxflow.graph_store import PostgresGraphStore
from ctxflow.models import Edge, Node
from researchos.graph_backend import (
    PostgresGraphBackend,
    graph_backend_choice,
)
from researchos.persistence import ResearchWorkspace


class _MemoryBackend:
    name = "fake-backend"

    def __init__(self) -> None:
        self.stored: dict[str, ContextGraph] = {}
        self.saves: list[str] = []
        self.fail_load = False

    def load(self, project_id: str) -> Optional[ContextGraph]:
        if self.fail_load:
            raise ConnectionError("db down")
        return self.stored.get(project_id)

    def save(self, project_id: str, graph: ContextGraph) -> None:
        self.saves.append(project_id)
        self.stored[project_id] = graph

    def close(self) -> None:
        pass


def _graph() -> ContextGraph:
    graph = ContextGraph()
    graph.add_node(Node(id="a", content="alpha"))
    graph.add_node(Node(id="b", content="beta"))
    graph.add_edge(Edge("a", "b", relation_type="cites", confidence=0.7))
    graph.add_edge(Edge("a", "b", relation_type="supports"))
    return graph


def test_workspace_loads_and_saves_through_backend(tmp_path):
    backend = _MemoryBackend()
    backend.stored["p"] = _graph()
    workspace = ResearchWorkspace(data_dir=tmp_path, graph_backend=backend)

    ctx = workspace.get_graph("p")
    assert ctx.graph.node_count == 2 and ctx.graph.edge_count == 2
    assert workspace.backend == "fake-backend"

    ctx.graph.add_node(Node(id="c", content="gamma"))
    workspace.save_graph("p")
    assert backend.saves == ["p"]
    assert backend.stored["p"].node_count == 3
    assert not (tmp_path / "projects" / "p" / "graph.json").exists()


def test_legacy_json_graph_is_migrated_once(tmp_path):
    json_workspace = ResearchWorkspace(data_dir=tmp_path)
    ctx = json_workspace.get_graph("legacy")
    ctx.graph.add_node(Node(id="a", content="alpha"))
    json_workspace.save_graph("legacy")

    backend = _MemoryBackend()
    workspace = ResearchWorkspace(data_dir=tmp_path, graph_backend=backend)
    assert workspace.get_graph("legacy").graph.node_count == 1
    assert backend.saves == ["legacy"]

    ResearchWorkspace(data_dir=tmp_path, graph_backend=backend).get_graph("legacy")
    assert backend.saves == ["legacy"]  # already stored; JSON no longer re-imported


def test_failed_backend_load_raises_instead_of_starting_empty(tmp_path):
    backend = _MemoryBackend()
    backend.fail_load = True
    workspace = ResearchWorkspace(data_dir=tmp_path, graph_backend=backend)

    with pytest.raises(ConnectionError):
        workspace.get_graph("p")
    assert backend.saves == []


def test_graph_backend_choice_validates():
    assert graph_backend_choice("") == "json"
    assert graph_backend_choice(" Postgres ") == "postgres"
    with pytest.raises(ValueError):
        graph_backend_choice("sqlite")


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConnection:
    """Stores jsonb rows in dicts and answers only the queries PostgresGraphStore issues."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], str] = {}
        self.edges: dict[tuple[str, str, str, str], str] = {}
        self.graphs: set[str] = set()
        self.statements: list[str] = []

    def transaction(self):
        return _Tx()

    async def execute(self, sql: str, *args: Any):
        self.statements.append(sql.split()[0] + " " + sql.split()[2])
        if sql.startswith("DELETE FROM project_graph_edges"):
            self.edges = {k: v for k, v in self.edges.items() if k[0] != args[0]}
        elif sql.startswith("DELETE FROM project_graph_nodes"):
            keep = set(args[1])
            self.nodes = {
                k: v for k, v in self.nodes.items() if k[0] != args[0] or k[1] in keep
            }
        elif "INTO project_graphs" in sql:
            self.graphs.add(args[0])

    async def executemany(self, sql: str, rows: list[tuple]):
        for row in rows:
            if "project_graph_nodes" in sql:
                self.nodes[(row[0], row[1])] = row[2]
            else:
                self.edges[tuple(row[:4])] = row[4]

    async def fetchrow(self, sql: str, project_id: str):
        return {"?column?": 1} if project_id in self.graphs else None

    async def fetch(self, sql: str, *args: Any):
        project_id = args[0]
        if "FROM project_graph_nodes" in sql:
            cursor, limit = args[1], args[2]
            rows = sorted(
                (k[1], v) for k, v in self.nodes.items()
                if k[0] == project_id and (cursor is None or k[1] > cursor)
            )[:limit]
            return [{"node_id": node_id, "data": data} for node_id, data in rows]
        src, dst, relation, limit = args[1:5]
        keys = sorted(
            k for k in self.edges
            if k[0] == project_id and (src is None or k[1:] > (src, dst, relation))
        )[:limit]
        return [
            {"src": k[1], "dst": k[2], "relation": k[3], "data": self.edges[k]}
            for k in keys
        ]


class _FakePool:
    def __init__(self) -> None:
        self.connection = _FakeConnection()

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.connection

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def test_postgres_store_round_trips_a_multigraph_across_pages():
    async def run():
        pool = _FakePool()
        store = PostgresGraphStore(pool, page_size=2)
        graph = ContextGraph()
        for index in range(5):
            graph.add_node(Node(id=f"n{index}", content=f"c{index}", tags={"t"}))
        graph.add_edge(Edge("n0", "n1", relation_type="cites", confidence=0.7))
        graph.add_edge(Edge("n0", "n1", relation_type="supports"))
        graph.add_edge(Edge("n1", "n2"))
        graph.add_edge(Edge("n3", "n4", relation_type="derived_from"))
        await store.save_graph("p", graph)
        return await store.load_graph("p"), await store.load_graph("other"), pool

    loaded, other, pool = asyncio.run(run())

    assert other is None
    assert loaded is not None
    assert loaded.node_count == 5 and loaded.edge_count == 4
    assert {e.relation_type for e in loaded.get_edges_between("n0", "n1")} == {
        "cites",
        "supports",
    }
    assert loaded.get_node("n2").tags == {"t"}
    cites = next(
        e for e in loaded.get_edges_between("n0", "n1") if e.relation_type == "cites"
    )
    assert cites.confidence == 0.7


def test_postgres_store_save_replaces_removed_nodes_and_edges():
    async def run():
        pool = _FakePool()
        store = PostgresGraphStore(pool)
        graph = ContextGraph()
        graph.add_node(Node(id="a", content="a"))
        graph.add_node(Node(id="b", content="b"))
        graph.add_edge(Edge("a", "b"))
        await store.save_graph("p", graph)

        graph.remove_node("b")
        await store.save_graph("p", graph)
        return await store.load_graph("p")

    loaded = asyncio.run(run())
    assert loaded is not None
    assert [n.id for n in loaded.all_nodes()] == ["a"]
    assert loaded.edge_count == 0


def test_postgres_store_rejects_bad_page_size():
    with pytest.raises(ValueError):
        PostgresGraphStore(_FakePool(), page_size=0)


def test_sync_backend_runs_store_calls_on_its_own_loop():
    import threading

    pool = _FakePool()
    pool.close = lambda: asyncio.sleep(0)  # type: ignore[attr-defined]
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    backend = PostgresGraphBackend(pool, loop, thread)

    async def in_api_loop():
        # Called from a running loop, as the route handlers do; must not deadlock.
        backend.save("p", _graph())
        return backend.load("p")

    loaded = asyncio.run(in_api_loop())
    backend.close()

    assert loaded is not None and loaded.edge_count == 2
    assert not thread.is_alive()
    assert json.loads(next(iter(pool.connection.nodes.values())))["id"] in {"a", "b"}
