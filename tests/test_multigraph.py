"""Typed multigraph: parallel relations, vocabulary registry, edge attributes."""

from __future__ import annotations

from ctxflow.graph import ContextGraph
from ctxflow.governance import GovernanceEngine
from ctxflow.models import CtxFlowConfig, Edge, Node
from ctxflow.serialization import _graph_to_payload, _payload_to_graph
from ctxflow.vocabulary import (
    RelationCategory,
    is_known_node_type,
    is_known_relation,
    register_relation,
    relation_category,
    relations_in,
)


def _graph() -> ContextGraph:
    graph = ContextGraph()
    for node_id in ("a", "b", "c"):
        graph.add_node(Node(id=node_id, content=node_id))
    return graph


def test_parallel_relations_coexist_between_one_pair():
    graph = _graph()
    graph.add_edge(Edge("a", "b", relation_type="cites"))
    graph.add_edge(Edge("a", "b", relation_type="supports"))

    assert graph.edge_count == 2
    assert {e.relation_type for e in graph.get_edges_between("a", "b")} == {
        "cites",
        "supports",
    }
    assert graph.has_edge("a", "b", "cites")
    assert not graph.has_edge("a", "b", "extends")


def test_same_triple_replaces_instead_of_duplicating():
    graph = _graph()
    graph.add_edge(Edge("a", "b", relation_type="cites", weight=0.2))
    graph.add_edge(Edge("a", "b", relation_type="cites", weight=0.9))

    edges = graph.get_edges_between("a", "b")
    assert len(edges) == 1
    assert edges[0].weight == 0.9


def test_remove_edge_targets_one_relation_or_all():
    graph = _graph()
    graph.add_edge(Edge("a", "b", relation_type="cites"))
    graph.add_edge(Edge("a", "b", relation_type="supports"))

    removed = graph.remove_edge("a", "b", "cites")
    assert removed is not None and removed.relation_type == "cites"
    assert [e.relation_type for e in graph.get_edges_between("a", "b")] == ["supports"]

    assert graph.remove_edge("a", "b", "cites") is None
    assert graph.remove_edge("a", "b") is not None
    assert graph.edge_count == 0


def test_edge_confidence_and_origin_round_trip():
    graph = _graph()
    graph.add_edge(
        Edge("a", "b", relation_type="cites", confidence=0.7, created_by="laya")
    )
    graph.add_edge(Edge("a", "b", relation_type="supports"))

    loaded = _payload_to_graph(_graph_to_payload(graph))

    by_relation = {e.relation_type: e for e in loaded.get_edges_between("a", "b")}
    assert by_relation["cites"].confidence == 0.7
    assert by_relation["cites"].created_by == "laya"
    assert by_relation["supports"].created_by == "rule"


def test_legacy_edge_payload_without_new_fields_loads():
    payload = {
        "nodes": [{"id": "a", "content": "a"}, {"id": "b", "content": "b"}],
        "edges": [{"source_id": "a", "target_id": "b", "relation_type": "cites"}],
    }
    edge = _payload_to_graph(payload).get_edges_between("a", "b")[0]
    assert (edge.confidence, edge.created_by) == (1.0, "rule")


def test_degree_cap_counts_parallel_relations_but_allows_replacement():
    graph = _graph()
    governance = GovernanceEngine(CtxFlowConfig(degree_cap=2))

    assert governance.add_edge(graph, Edge("a", "b", relation_type="cites"))
    assert governance.add_edge(graph, Edge("a", "b", relation_type="supports"))
    assert not governance.add_edge(graph, Edge("a", "c", relation_type="cites"))
    # Replacing an existing triple never grows the fan-out.
    assert governance.add_edge(graph, Edge("a", "b", relation_type="cites", weight=0.5))


def test_provenance_walk_uses_registry_categories():
    graph = _graph()
    graph.add_edge(Edge("c", "b", relation_type="derived_from"))
    graph.add_edge(Edge("c", "b", relation_type="related_to"))
    graph.add_edge(Edge("b", "a", relation_type="supersedes"))

    chain = graph.trace_provenance("b")

    assert [step.node.id for step in chain.steps] == ["b", "c"]
    assert [n.id for n, _ in chain.contradictions] == ["a"]


def test_registry_classifies_and_extends():
    assert relation_category("supersedes") is RelationCategory.CONTRADICTION
    assert relation_category("cites") is RelationCategory.RESEARCH
    assert relation_category("unknown_rel") is None
    assert is_known_relation("derived_from")
    assert "derived_from" in relations_in(RelationCategory.PROVENANCE)
    assert is_known_node_type("claim") and not is_known_node_type("zebra")

    register_relation("replicates", RelationCategory.RESEARCH, "Replicates a result")
    assert "replicates" in relations_in(RelationCategory.RESEARCH)
