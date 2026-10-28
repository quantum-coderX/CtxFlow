"""Offline graph maintenance operations."""

from __future__ import annotations

from dataclasses import dataclass

from ctxflow.extractors import DefaultExtractor, Extractor
from ctxflow.graph import ContextGraph
from ctxflow.models import CtxFlowConfig
from ctxflow.pruning import Pruner


@dataclass(frozen=True)
class CompactionResult:
    """Summary of one offline compaction pass."""

    removed_node_ids: tuple[str, ...]
    nodes_before: int
    nodes_after: int


def compact_graph(
    graph: ContextGraph,
    *,
    threshold: float = 0.5,
    strategy: str = "summarize",
    config: CtxFlowConfig | None = None,
    extractor: Extractor | None = None,
) -> CompactionResult:
    """Run pruning as an explicit maintenance operation.

    This helper is intentionally not called from query or ingestion paths.
    """

    nodes_before = graph.node_count
    pruner = Pruner(config or CtxFlowConfig(), extractor or DefaultExtractor())
    removed = tuple(pruner.prune(graph, threshold=threshold, strategy=strategy))
    return CompactionResult(
        removed_node_ids=removed,
        nodes_before=nodes_before,
        nodes_after=graph.node_count,
    )
