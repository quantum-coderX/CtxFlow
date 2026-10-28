"""CtxFlow — Schema-agnostic graph-based context management for LLM agents.

Public API surface:

- :class:`CtxFlow` — the primary facade (``ingest``, ``query``, ``prune``,
  ``research_trace``, ``export``, ``import_``).
- :class:`CtxFlowConfig` — typed configuration with sensible defaults.
- :class:`Node`, :class:`Edge`, :class:`QueryResult` — core data models.
- :class:`NodeStatus` — node validity status enum.
- :class:`ContradictionResult` — result of contradiction detection.
- :class:`ProvenanceChain`, :class:`ResearchTrace` — provenance types.
- :class:`Extractor`, :class:`DefaultExtractor` — pluggable extraction.
- :class:`LLMExtractor` — LLM-powered extraction.
- :class:`ContradictionDetector` — supersession/contradiction detection.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ctxflow.contradiction import ContradictionDetector, LLMCallable
from ctxflow.extraction_cache import ExtractionCache, InMemoryExtractionCache
from ctxflow.extractors import DefaultExtractor, Extractor
from ctxflow.governance import GovernanceEngine
from ctxflow.graph import ContextGraph
from ctxflow.llm_extractor import LLMExtractor
from ctxflow.models import (
    ContradictionResult,
    CtxFlowConfig,
    Edge,
    Node,
    NodeStatus,
    ProvenanceChain,
    ProvenanceStep,
    QueryResult,
    ResearchTrace,
)
from ctxflow.pruning import Pruner
from ctxflow.query import QueryEngine
from ctxflow.serialization import (
    export_binary,
    export_json,
    import_binary,
    import_json,
)

__all__ = [
    "CtxFlow",
    "CtxFlowConfig",
    "Node",
    "Edge",
    "NodeStatus",
    "QueryResult",
    "ContradictionResult",
    "ProvenanceChain",
    "ProvenanceStep",
    "ResearchTrace",
    "Extractor",
    "DefaultExtractor",
    "LLMExtractor",
    "ContradictionDetector",
    "ExtractionCache",
    "InMemoryExtractionCache",
]


class CtxFlow:
    """High-level facade for the CtxFlow context graph.

    Composes the graph, extractor, governance engine, query engine,
    pruner, and contradiction detector behind a clean API::

        ctx = CtxFlow()
        node_id = ctx.ingest("The agent observed a wall ahead.")
        results = ctx.query("What obstacles are nearby?", k=3)
        trace = ctx.research_trace(node_id)
        removed = ctx.prune(threshold=0.7, strategy="drop")
        ctx.export("memory.json")

    Parameters
    ----------
    config : CtxFlowConfig | None
        Framework configuration. Defaults are used if omitted.
    extractor : Extractor | None
        Pluggable text extractor. Falls back to :class:`DefaultExtractor`.
    llm : LLMCallable | None
        An LLM callable ``(prompt, system) -> str`` used for:
        - contradiction detection (when ``config.contradiction_detection``
          is ``True``)
        - :class:`LLMExtractor` (if no explicit *extractor* is given and
          an *llm* is provided).
    """

    def __init__(
        self,
        config: CtxFlowConfig | None = None,
        extractor: Extractor | None = None,
        llm: Optional[Callable] = None,
    ) -> None:
        self._config = config or CtxFlowConfig()
        self._config.validate()

        # If an LLM is provided but no extractor, use LLMExtractor with
        # a DefaultExtractor fallback for resilience.
        if extractor is not None:
            self._extractor = extractor
        elif llm is not None:
            self._extractor = LLMExtractor(
                llm=llm, fallback=DefaultExtractor()
            )
        else:
            self._extractor = DefaultExtractor()

        self._graph = ContextGraph()
        self._governance = GovernanceEngine(self._config)
        self._query_engine = QueryEngine(self._config, self._extractor, self._governance)
        self._pruner = Pruner(self._config, self._extractor)

        # Contradiction detector (uses the same LLM callable).
        self._contradiction_detector = ContradictionDetector(
            config=self._config, llm=llm
        )

    # -- Properties (power-user access) -------------------------------------

    @property
    def config(self) -> CtxFlowConfig:
        """Current framework configuration."""
        return self._config

    @property
    def graph(self) -> ContextGraph:
        """Direct access to the underlying ContextGraph."""
        return self._graph

    @property
    def extractor(self) -> Extractor:
        """The active Extractor instance."""
        return self._extractor

    @property
    def contradiction_detector(self) -> ContradictionDetector:
        """The ContradictionDetector instance."""
        return self._contradiction_detector

    # -- Ingestion ----------------------------------------------------------

    def ingest(
        self,
        content: str,
        node_type: str | None = None,
        source_id: str | None = None,
        tags: Set[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        relation_type: str | None = None,
        check_contradictions: bool | None = None,
    ) -> str:
        """Ingest a context unit into the graph.

        Parameters
        ----------
        content : str
            Raw text content of the context unit.
        node_type : str | None
            Free-form type label (e.g. ``"observation"``, ``"decision"``).
        source_id : str | None
            If provided, creates an explicit edge from *source_id* to the
            new node with the given *relation_type* (default ``"follows"``).
        tags : set[str] | None
            Caller-supplied tags. If ``None``, tags are auto-extracted
            via the configured Extractor.
        metadata : dict | None
            Arbitrary key-value pairs attached to the node.
        relation_type : str | None
            Relation type for the explicit edge (default ``"follows"``).
        check_contradictions : bool | None
            Override ``config.contradiction_detection`` for this call.
            If ``None``, uses the config default.

        Returns
        -------
        str
            The ID of the newly created node.
        """
        # 1. Extract tags and summary.
        extracted_tags = self._extractor.extract_tags(content)
        if tags:
            extracted_tags = extracted_tags | tags
        summary = self._extractor.summarize(content)

        # 2. Create and insert the node.
        node = Node(
            content=content,
            summary=summary,
            tags=extracted_tags,
            node_type=node_type or "generic",
            metadata=metadata or {},
        )
        self._graph.add_node(node)

        # 3. Explicit link (if source_id provided).
        edge_relation = relation_type or "follows"
        if source_id and self._graph.has_node(source_id):
            edge = Edge(
                source_id=source_id,
                target_id=node.id,
                relation_type=edge_relation,
                weight=1.0,
            )
            self._governance.add_edge(self._graph, edge)

        # 4. Implicit links (top-k tag-similar nodes).
        if self._config.max_link_k > 0:
            similar = self._graph.find_similar_by_tags(
                tags=node.tags,
                k=self._config.max_link_k,
                min_similarity=self._config.min_link_similarity,
                exclude={node.id},
            )
            for similar_node, sim_score in similar:
                # Check degree cap before linking.
                edge = Edge(
                    source_id=node.id,
                    target_id=similar_node.id,
                    relation_type="related_to",
                    weight=sim_score,
                )
                self._governance.add_edge(self._graph, edge)

        # 5. Contradiction detection (if enabled).
        should_check = check_contradictions if check_contradictions is not None \
            else self._config.contradiction_detection
        if should_check:
            self._contradiction_detector.detect_and_apply(
                self._graph, node
            )

        return node.id

    # -- Query --------------------------------------------------------------

    def query(
        self,
        context: str,
        k: int = 5,
        weights: Tuple[float, float, float] | None = None,
        anchor: str | None = None,
        include_superseded: bool = False,
    ) -> List[QueryResult]:
        """Retrieve the top-*k* most relevant nodes for *context*.

        Parameters
        ----------
        context : str
            Natural-language query (tags auto-extracted via Extractor).
        k : int
            Number of results.
        weights : tuple[float, float, float] | None
            Override ``(alpha, beta, gamma)`` for this query.
        anchor : str | None
            Node ID for proximity scoring (BFS anchor).
        include_superseded : bool
            If ``False`` (default), superseded nodes are excluded.

        Returns
        -------
        list[QueryResult]
            Ranked results with score breakdowns.
        """
        return self._query_engine.query(
            graph=self._graph,
            context=context,
            k=k,
            weights=weights,
            anchor=anchor,
            include_superseded=include_superseded,
        )

    # -- Provenance / Research Trace ----------------------------------------

    def research_trace(self, node_id: str) -> ResearchTrace:
        """Build a full research trace for a node.

        Returns the provenance chain (backward walk through ``follows``,
        ``derived_from``, ``extracted_from``, ``references`` edges), plus
        all superseded/contradicting claims and source paper nodes found
        along the chain.

        Parameters
        ----------
        node_id : str
            The node to trace (typically a claim or answer node).

        Returns
        -------
        ResearchTrace
            Full provenance and contradiction context.
        """
        chain = self._graph.trace_provenance(node_id)

        # Classify nodes in the chain.
        superseded: List[Node] = []
        source_papers: List[Node] = []
        seen_superseded = set()

        for step in chain.steps:
            if step.node.status == NodeStatus.SUPERSEDED:
                if step.node.id not in seen_superseded:
                    superseded.append(step.node)
                    seen_superseded.add(step.node.id)
            if step.node.node_type in ("paper", "reference", "source"):
                source_papers.append(step.node)

        # A claim that *supersedes* an older claim must surface the older node
        # even though it is not part of the backward provenance chain. The
        # traced node itself is included because the chain only walks parents.
        sources = {node_id} | {step.node.id for step in chain.steps}
        for source_id in sources:
            for edge in self._graph.get_edges_from(source_id):
                if edge.relation_type != "supersedes":
                    continue
                target = self._graph.get_node(edge.target_id)
                if target is not None and target.id not in seen_superseded:
                    superseded.append(target)
                    seen_superseded.add(target.id)

        return ResearchTrace(
            answer_node_id=node_id,
            supporting_chain=chain,
            superseded_claims=superseded,
            contradicting_claims=chain.contradictions,
            source_papers=source_papers,
        )

    # -- Pruning ------------------------------------------------------------

    def prune(
        self,
        threshold: float = 0.5,
        strategy: str = "drop",
    ) -> List[str]:
        """Prune stale nodes from the graph.

        Parameters
        ----------
        threshold : float
            Staleness threshold (0–1). Nodes above this are pruned.
        strategy : str
            ``"drop"`` to delete, ``"summarize"`` to collapse clusters.

        Returns
        -------
        list[str]
            IDs of removed/replaced nodes.
        """
        return self._pruner.prune(
            graph=self._graph,
            threshold=threshold,
            strategy=strategy,
        )

    # -- Persistence --------------------------------------------------------

    def export(self, path: str, format: str = "json") -> None:
        """Serialize the graph to disk.

        Parameters
        ----------
        path : str
            File path to write to.
        format : str
            ``"json"`` (default) or ``"binary"`` (MessagePack).
        """
        if format == "json":
            export_json(self._graph, path)
        elif format == "binary":
            export_binary(self._graph, path)
        else:
            raise ValueError(f"Unknown format: {format!r}. Use 'json' or 'binary'.")

    def import_(self, path: str, format: str = "json") -> None:
        """Load a graph from disk, replacing the current graph.

        Parameters
        ----------
        path : str
            File path to read from.
        format : str
            ``"json"`` (default) or ``"binary"`` (MessagePack).
        """
        if format == "json":
            self._graph = import_json(path)
        elif format == "binary":
            self._graph = import_binary(path)
        else:
            raise ValueError(f"Unknown format: {format!r}. Use 'json' or 'binary'.")
        # Rebuild internal references.
        self._query_engine = QueryEngine(
            self._config, self._extractor, self._governance
        )
        self._pruner = Pruner(self._config, self._extractor)

    # -- Convenience --------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"CtxFlow(nodes={self._graph.node_count}, "
            f"edges={self._graph.edge_count})"
        )

    def __len__(self) -> int:
        return self._graph.node_count
