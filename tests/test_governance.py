"""Tests for GovernanceEngine — degree caps and score normalization."""

from ctxflow.governance import GovernanceEngine
from ctxflow.graph import ContextGraph
from ctxflow.models import CtxFlowConfig, Edge, Node


def _make_node(id: str) -> Node:
    return Node(content=f"content-{id}", id=id)


class TestDegreeCap:
    def test_cap_allows_under_limit(self):
        config = CtxFlowConfig(degree_cap=2, degree_cap_enabled=True)
        gov = GovernanceEngine(config)
        g = ContextGraph()
        g.add_node(_make_node("a"))
        g.add_node(_make_node("b"))
        g.add_edge(Edge(source_id="a", target_id="b"))
        assert gov.can_add_edge(g, "a") is True  # 1 < 2

    def test_cap_rejects_at_limit(self):
        config = CtxFlowConfig(degree_cap=1, degree_cap_enabled=True)
        gov = GovernanceEngine(config)
        g = ContextGraph()
        g.add_node(_make_node("a"))
        g.add_node(_make_node("b"))
        g.add_node(_make_node("c"))
        g.add_edge(Edge(source_id="a", target_id="b"))
        assert gov.can_add_edge(g, "a") is False  # 1 >= 1

    def test_cap_disabled(self):
        config = CtxFlowConfig(degree_cap=1, degree_cap_enabled=False)
        gov = GovernanceEngine(config)
        g = ContextGraph()
        g.add_node(_make_node("a"))
        g.add_node(_make_node("b"))
        g.add_node(_make_node("c"))
        g.add_edge(Edge(source_id="a", target_id="b"))
        g.add_edge(Edge(source_id="a", target_id="c"))
        assert gov.can_add_edge(g, "a") is True  # Cap disabled.

    def test_edge_writer_enforces_cap(self):
        config = CtxFlowConfig(degree_cap=2, degree_cap_enabled=True)
        gov = GovernanceEngine(config)
        g = ContextGraph()
        for node_id in ("a", "b", "c", "d"):
            g.add_node(_make_node(node_id))

        assert gov.add_edge(g, Edge("a", "b"))
        assert gov.add_edge(g, Edge("a", "c"))
        assert not gov.add_edge(g, Edge("a", "d"))
        assert g.out_degree("a") == 2

    def test_random_edge_writes_never_exceed_cap(self):
        config = CtxFlowConfig(degree_cap=7, degree_cap_enabled=True)
        gov = GovernanceEngine(config)
        g = ContextGraph()
        node_ids = [str(index) for index in range(50)]
        for node_id in node_ids:
            g.add_node(_make_node(node_id))

        for index in range(1000):
            source = node_ids[(index * 17) % len(node_ids)]
            target = node_ids[(index * 31 + 1) % len(node_ids)]
            if source != target:
                gov.add_edge(g, Edge(source, target, relation_type=f"r{index}"))

        assert all(g.out_degree(node_id) <= config.degree_cap for node_id in node_ids)


class TestScoreNormalization:
    def test_normalization_enabled(self):
        config = CtxFlowConfig(degree_normalization=True)
        gov = GovernanceEngine(config)
        # score / (degree + 1) = 1.0 / (5 + 1) = 1/6
        result = gov.normalize_score(1.0, degree=5)
        assert abs(result - 1 / 6) < 1e-9

    def test_normalization_disabled(self):
        config = CtxFlowConfig(degree_normalization=False)
        gov = GovernanceEngine(config)
        assert gov.normalize_score(0.8, degree=100) == 0.8
