"""Tests for ContextGraph — node/edge CRUD, BFS, tag retrieval."""

import pytest

from ctxflow.graph import ContextGraph
from ctxflow.models import Edge, Node


def _make_node(id: str, tags: set[str] | None = None, **kwargs) -> Node:
    return Node(content=f"content-{id}", id=id, tags=tags or set(), **kwargs)


class TestNodeOperations:
    def test_add_and_get(self):
        g = ContextGraph()
        node = _make_node("n1", tags={"a", "b"})
        g.add_node(node)
        assert g.node_count == 1
        assert g.get_node("n1") is node

    def test_get_missing(self):
        g = ContextGraph()
        assert g.get_node("missing") is None

    def test_has_node(self):
        g = ContextGraph()
        g.add_node(_make_node("n1"))
        assert g.has_node("n1")
        assert not g.has_node("n2")

    def test_remove_node(self):
        g = ContextGraph()
        g.add_node(_make_node("n1", tags={"x"}))
        removed = g.remove_node("n1")
        assert removed is not None
        assert g.node_count == 0
        assert g.get_node("n1") is None

    def test_remove_missing_node(self):
        g = ContextGraph()
        assert g.remove_node("missing") is None

    def test_remove_cleans_tag_index(self):
        g = ContextGraph()
        g.add_node(_make_node("n1", tags={"tag1"}))
        g.remove_node("n1")
        # Tag index should be clean — no results for "tag1".
        results = g.find_similar_by_tags({"tag1"}, k=5)
        assert len(results) == 0

    def test_all_nodes(self):
        g = ContextGraph()
        g.add_node(_make_node("a"))
        g.add_node(_make_node("b"))
        g.add_node(_make_node("c"))
        nodes = g.all_nodes()
        assert len(nodes) == 3
        ids = {n.id for n in nodes}
        assert ids == {"a", "b", "c"}


class TestEdgeOperations:
    def setup_method(self):
        self.g = ContextGraph()
        self.g.add_node(_make_node("a"))
        self.g.add_node(_make_node("b"))
        self.g.add_node(_make_node("c"))

    def test_add_edge(self):
        edge = Edge(source_id="a", target_id="b")
        assert self.g.add_edge(edge) is True
        assert self.g.edge_count == 1

    def test_add_edge_missing_node(self):
        edge = Edge(source_id="a", target_id="missing")
        assert self.g.add_edge(edge) is False

    def test_remove_edge_returns_removed_edge(self):
        edge = Edge(source_id="a", target_id="b", relation_type="supports")
        self.g.add_edge(edge)
        removed = self.g.remove_edge("a", "b")
        assert removed is not None
        assert removed.relation_type == "supports"
        assert self.g.edge_count == 0
        assert self.g.edge_count == 0

    def test_get_edges_from(self):
        self.g.add_edge(Edge(source_id="a", target_id="b", relation_type="r1"))
        self.g.add_edge(Edge(source_id="a", target_id="c", relation_type="r2"))
        edges = self.g.get_edges_from("a")
        assert len(edges) == 2
        targets = {e.target_id for e in edges}
        assert targets == {"b", "c"}

    def test_get_edges_to(self):
        self.g.add_edge(Edge(source_id="a", target_id="c"))
        self.g.add_edge(Edge(source_id="b", target_id="c"))
        edges = self.g.get_edges_to("c")
        assert len(edges) == 2
        sources = {e.source_id for e in edges}
        assert sources == {"a", "b"}

    def test_out_degree(self):
        self.g.add_edge(Edge(source_id="a", target_id="b"))
        self.g.add_edge(Edge(source_id="a", target_id="c"))
        assert self.g.out_degree("a") == 2
        assert self.g.out_degree("b") == 0

    def test_in_degree(self):
        self.g.add_edge(Edge(source_id="a", target_id="c"))
        self.g.add_edge(Edge(source_id="b", target_id="c"))
        assert self.g.in_degree("c") == 2

    def test_total_degree(self):
        self.g.add_edge(Edge(source_id="a", target_id="b"))
        self.g.add_edge(Edge(source_id="c", target_id="a"))
        assert self.g.total_degree("a") == 2  # 1 out + 1 in

    def test_remove_node_removes_edges(self):
        self.g.add_edge(Edge(source_id="a", target_id="b"))
        self.g.add_edge(Edge(source_id="b", target_id="c"))
        self.g.remove_node("b")
        assert self.g.edge_count == 0


class TestBFS:
    def test_linear_chain(self):
        g = ContextGraph()
        for i in range(5):
            g.add_node(_make_node(str(i)))
        for i in range(4):
            g.add_edge(Edge(source_id=str(i), target_id=str(i + 1)))

        distances = g.bfs_distances("0", max_depth=3)
        assert distances["1"] == 1
        assert distances["2"] == 2
        assert distances["3"] == 3
        assert "4" not in distances  # Beyond max_depth=3.

    def test_bfs_undirected(self):
        """BFS should follow both forward and backward edges."""
        g = ContextGraph()
        g.add_node(_make_node("a"))
        g.add_node(_make_node("b"))
        g.add_edge(Edge(source_id="a", target_id="b"))

        # Starting from b, should reach a via the incoming edge.
        distances = g.bfs_distances("b", max_depth=3)
        assert "a" in distances
        assert distances["a"] == 1

    def test_bfs_missing_node(self):
        g = ContextGraph()
        assert g.bfs_distances("missing") == {}

    def test_bfs_isolated_node(self):
        g = ContextGraph()
        g.add_node(_make_node("alone"))
        assert g.bfs_distances("alone") == {}


class TestTagRetrieval:
    def test_find_similar(self):
        g = ContextGraph()
        g.add_node(_make_node("n1", tags={"python", "code", "agent"}))
        g.add_node(_make_node("n2", tags={"python", "code"}))
        g.add_node(_make_node("n3", tags={"javascript", "web"}))

        results = g.find_similar_by_tags({"python", "agent"}, k=5)
        assert len(results) >= 1
        # n1 should score highest (2/3 overlap with query).
        assert results[0][0].id == "n1"

    def test_find_similar_min_similarity(self):
        g = ContextGraph()
        g.add_node(_make_node("n1", tags={"a", "b", "c", "d"}))
        results = g.find_similar_by_tags({"a"}, k=5, min_similarity=0.5)
        assert len(results) == 0  # Jaccard("a", "a,b,c,d") = 1/4 = 0.25 < 0.5

    def test_find_similar_exclude(self):
        g = ContextGraph()
        g.add_node(_make_node("n1", tags={"x"}))
        g.add_node(_make_node("n2", tags={"x"}))
        results = g.find_similar_by_tags({"x"}, k=5, exclude={"n1"})
        assert len(results) == 1
        assert results[0][0].id == "n2"

    def test_find_similar_empty_tags(self):
        g = ContextGraph()
        g.add_node(_make_node("n1", tags={"a"}))
        assert g.find_similar_by_tags(set(), k=5) == []

    def test_jaccard(self):
        assert ContextGraph.jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
        assert ContextGraph.jaccard({"a"}, {"a"}) == 1.0
        assert ContextGraph.jaccard(set(), {"a"}) == 0.0
        assert ContextGraph.jaccard(set(), set()) == 0.0
