"""Graph governance — degree caps and score normalization.

Enforces structural health constraints at insertion time (degree cap)
and optionally at query time (score normalization by degree). This is
the explicit mitigation of Mem0g's high-degree bias problem.
"""

from __future__ import annotations

from ctxflow.graph import ContextGraph
from ctxflow.models import CtxFlowConfig, Edge


class GovernanceEngine:
    """Stateless governance checks operating over a ContextGraph."""

    def __init__(self, config: CtxFlowConfig) -> None:
        self.config = config

    def can_add_edge(self, graph: ContextGraph, source_id: str) -> bool:
        """Return ``True`` if *source_id* can accept another outgoing edge.

        When ``degree_cap_enabled`` is ``False``, always returns ``True``.
        Otherwise, checks that the node's out-degree is below the cap.
        """
        if not self.config.degree_cap_enabled:
            return True
        return graph.out_degree(source_id) < self.config.degree_cap

    def add_edge(self, graph: ContextGraph, edge: Edge) -> bool:
        """Add an edge only when its source remains within the degree cap."""

        if not graph.has_node(edge.source_id) or not graph.has_node(edge.target_id):
            return False
        replacing = any(
            existing.target_id == edge.target_id
            for existing in graph.get_edges_from(edge.source_id)
        )
        if not replacing and not self.can_add_edge(graph, edge.source_id):
            return False
        return graph.add_edge(edge)

    def normalize_score(self, score: float, degree: int) -> float:
        """Optionally normalize *score* by the node's total degree.

        When ``degree_normalization`` is enabled, returns
        ``score / (degree + 1)`` to dampen high-degree nodes.
        Otherwise returns *score* unchanged.
        """
        if not self.config.degree_normalization:
            return score
        return score / (degree + 1)
