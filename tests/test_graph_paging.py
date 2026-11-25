"""Keyset paging over the project graph (ContextGraph and /api/graph/page)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from ctxflow.graph import ContextGraph
from ctxflow.models import Edge, Node
from researchos.api.server import create_app
from researchos.persistence import ResearchWorkspace


def _graph(count: int) -> ContextGraph:
    graph = ContextGraph()
    for index in range(count):
        graph.add_node(Node(id=f"n{index:03d}", content=f"content {index}"))
    return graph


def test_page_nodes_walks_every_node_once():
    graph = _graph(25)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        nodes, cursor = graph.page_nodes(after=cursor, limit=10)
        seen.extend(node.id for node in nodes)
        pages += 1
        if cursor is None:
            break

    assert pages == 3
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 25


def test_page_nodes_exact_multiple_has_no_trailing_empty_page():
    nodes, cursor = _graph(10).page_nodes(limit=10)
    assert len(nodes) == 10
    assert cursor is None


def test_page_nodes_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        _graph(1).page_nodes(limit=0)


def test_graph_page_endpoint_is_bounded_and_complete(tmp_path):
    workspace = ResearchWorkspace(data_dir=tmp_path)
    ctxflow = workspace.get_graph("paged")
    for index in range(7):
        ctxflow.graph.add_node(Node(id=f"n{index}", content=f"content {index}"))
    ctxflow.graph.add_edge(Edge(source_id="n0", target_id="n6", relation_type="cites"))

    with TestClient(create_app(workspace=workspace)) as client:
        ids: list[str] = []
        edges = []
        cursor = None
        while True:
            params = {"project_id": "paged", "limit": 3}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/graph/page", params=params).json()
            assert len(body["nodes"]) <= 3
            assert body["total_nodes"] == 7
            ids.extend(node["id"] for node in body["nodes"])
            edges.extend(body["edges"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert client.get(
            "/api/graph/page", params={"project_id": "paged", "limit": 0}
        ).status_code == 422

    assert ids == [f"n{i}" for i in range(7)]
    assert [(e["source_id"], e["target_id"]) for e in edges] == [("n0", "n6")]
