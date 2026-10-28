"""Atomic supersession tests for the contradiction writer."""

import pytest

from ctxflow.contradiction import ContradictionDetector
from ctxflow.graph import ContextGraph
from ctxflow.models import (
    ContradictionResult,
    CtxFlowConfig,
    Node,
    NodeStatus,
)


def _graph() -> ContextGraph:
    graph = ContextGraph()
    graph.add_node(Node(id="new", content="new claim"))
    graph.add_node(Node(id="old", content="old claim"))
    return graph


def test_supersession_updates_edge_and_status_atomically():
    graph = _graph()
    detector = ContradictionDetector(CtxFlowConfig(), llm=lambda *_args: "")
    result = ContradictionResult(
        existing_node_id="old",
        relation="supersedes",
        confidence=0.9,
        evidence="new evidence",
    )

    edge = detector.apply_supersession(graph, "new", "old", result)

    assert edge is not None
    assert graph.get_edges_from("new")[0].relation_type == "supersedes"
    old = graph.get_node("old")
    assert old is not None
    assert old.status == NodeStatus.SUPERSEDED
    assert old.superseded_by == "new"


def test_supersession_rolls_back_when_edge_write_fails_after_mutation():
    graph = _graph()
    detector = ContradictionDetector(CtxFlowConfig(), llm=lambda *_args: "")
    original_add_edge = graph.add_edge

    def add_then_fail(edge):
        original_add_edge(edge)
        raise RuntimeError("injected edge failure")

    graph.add_edge = add_then_fail
    result = ContradictionResult(
        existing_node_id="old",
        relation="supersedes",
        confidence=0.9,
        evidence="new evidence",
    )

    with pytest.raises(RuntimeError, match="injected edge failure"):
        detector.apply_supersession(graph, "new", "old", result)

    assert graph.edge_count == 0
    old = graph.get_node("old")
    assert old is not None
    assert old.status == NodeStatus.ACTIVE
    assert old.superseded_by is None
