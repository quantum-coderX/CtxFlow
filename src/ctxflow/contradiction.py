"""Contradiction and supersession detection for CtxFlow.

This module implements the core research contribution: detecting when a
newly ingested claim contradicts or supersedes an existing claim in the
graph. The detector uses a two-stage pipeline:

1. **Candidate filtering** — finds nodes with high tag similarity to the
   new node (cheap, no LLM call).
2. **Semantic checking** — asks an LLM whether the new claim supersedes,
   contradicts, supports, or is unrelated to each candidate (expensive,
   batched).

The detector is injected with an :class:`LLMCallable` so it works with
any LLM provider (OpenAI, Gemini, local models, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Protocol, Set

from ctxflow.graph import ContextGraph
from ctxflow.governance import GovernanceEngine
from ctxflow.models import (
    ContradictionResult,
    CtxFlowConfig,
    Edge,
    Node,
    NodeStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM callable protocol
# ---------------------------------------------------------------------------

class LLMCallable(Protocol):
    """Protocol for an LLM function.

    Any callable matching this signature can be used::

        def my_llm(prompt: str, system: str = "") -> str:
            ...

    Works with plain functions, ``functools.partial``, lambdas, or
    class instances with ``__call__``.
    """

    def __call__(self, prompt: str, system: str = "") -> str: ...


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a research-claim analysis assistant. Given two claims, determine "
    "their relationship. Respond ONLY with a valid JSON object (no markdown "
    "fencing). The JSON must have exactly three keys:\n"
    '  "relation": one of "supersedes", "contradicts", "supports", "unrelated"\n'
    '  "confidence": a float between 0.0 and 1.0\n'
    '  "evidence": a one-sentence explanation\n'
    "\n"
    "Definitions:\n"
    '- "supersedes": Claim B presents a newer, more accurate finding that '
    "replaces Claim A.\n"
    '- "contradicts": Claim B directly conflicts with Claim A, but neither '
    "is clearly newer.\n"
    '- "supports": Claim B provides additional evidence for Claim A.\n'
    '- "unrelated": The claims address different topics.\n'
)

_USER_PROMPT_TEMPLATE = (
    "Claim A (existing, {a_type}):\n{a_content}\n\n"
    "Claim B (new, {b_type}):\n{b_content}\n\n"
    "Determine the relationship of Claim B to Claim A."
)


# ---------------------------------------------------------------------------
# ContradictionDetector
# ---------------------------------------------------------------------------

class ContradictionDetector:
    """Detects when a new claim contradicts or supersedes existing ones.

    Parameters
    ----------
    config : CtxFlowConfig
        Framework configuration (uses contradiction thresholds).
    llm : LLMCallable | None
        A callable ``(prompt, system) -> str`` that queries an LLM.
        If ``None``, contradiction detection is disabled and all methods
        return empty results.
    """

    def __init__(
        self,
        config: CtxFlowConfig,
        llm: Optional[LLMCallable] = None,
    ) -> None:
        self.config = config
        self._llm = llm
        self._governance = GovernanceEngine(config)

    @property
    def enabled(self) -> bool:
        """Whether contradiction detection is active."""
        return self._llm is not None and self.config.contradiction_detection

    # -- Public API ---------------------------------------------------------

    def find_candidates(
        self,
        graph: ContextGraph,
        new_node: Node,
        *,
        max_candidates: int = 10,
    ) -> List[Node]:
        """Find existing nodes that might contradict *new_node*.

        Uses the graph's tag index for fast pre-filtering. Only nodes whose
        tag Jaccard similarity exceeds
        ``config.contradiction_similarity_threshold`` are returned.

        Parameters
        ----------
        graph : ContextGraph
            The graph to search.
        new_node : Node
            The newly ingested node.
        max_candidates : int
            Maximum number of candidates to return.

        Returns
        -------
        list[Node]
            Candidate nodes sorted by descending tag similarity.
        """
        if not new_node.tags:
            return []

        similar = graph.find_similar_by_tags(
            tags=new_node.tags,
            k=max_candidates,
            min_similarity=self.config.contradiction_similarity_threshold,
            exclude={new_node.id},
        )
        # Only return active nodes — no point checking already-superseded ones.
        return [
            node for node, _sim in similar
            if node.status == NodeStatus.ACTIVE
        ]

    def check_contradictions(
        self,
        new_node: Node,
        candidates: List[Node],
    ) -> List[ContradictionResult]:
        """Ask the LLM whether *new_node* contradicts each candidate.

        Parameters
        ----------
        new_node : Node
            The newly ingested claim.
        candidates : list[Node]
            Existing nodes to check against.

        Returns
        -------
        list[ContradictionResult]
            One result per candidate, filtered to only include results
            where the LLM detected a non-``"unrelated"`` relation.
        """
        if not self._llm or not candidates:
            return []

        results: List[ContradictionResult] = []
        for candidate in candidates:
            result = self._check_pair(new_node, candidate)
            if result and result.relation != "unrelated":
                results.append(result)

        return results

    def apply_supersession(
        self,
        graph: ContextGraph,
        new_node_id: str,
        old_node_id: str,
        result: ContradictionResult,
    ) -> Optional[Edge]:
        """Create a supersession/contradiction edge and update node status.

        Parameters
        ----------
        graph : ContextGraph
            The graph to mutate.
        new_node_id : str
            ID of the new (superseding) node.
        old_node_id : str
            ID of the old (superseded) node.
        result : ContradictionResult
            The contradiction check result containing relation type,
            confidence, and evidence.

        Returns
        -------
        Edge | None
            The created edge, or ``None`` if nodes are missing.
        """
        old_node = graph.get_node(old_node_id)
        if old_node is None:
            return None

        # Create the edge: new_node --supersedes/contradicts--> old_node
        edge = Edge(
            source_id=new_node_id,
            target_id=old_node_id,
            relation_type=result.relation,
            weight=result.confidence,
            metadata={
                "confidence": result.confidence,
                "evidence": result.evidence,
            },
        )

        old_status = old_node.status
        old_superseded_by = old_node.superseded_by
        try:
            if not self._governance.add_edge(graph, edge):
                return None
            if result.relation == "supersedes":
                old_node.status = NodeStatus.SUPERSEDED
                old_node.superseded_by = new_node_id
            elif result.relation == "contradicts":
                old_node.status = NodeStatus.CONTESTED
        except Exception:
            graph.remove_edge(new_node_id, old_node_id)
            old_node.status = old_status
            old_node.superseded_by = old_superseded_by
            raise

        logger.info(
            "Applied %s: %s -> %s (confidence=%.2f)",
            result.relation, new_node_id, old_node_id, result.confidence,
        )
        return edge

    def detect_and_apply(
        self,
        graph: ContextGraph,
        new_node: Node,
        *,
        max_candidates: int = 10,
    ) -> List[ContradictionResult]:
        """Full pipeline: find candidates, check contradictions, apply edges.

        This is the convenience method called during ``CtxFlow.ingest()``
        when contradiction detection is enabled.

        Parameters
        ----------
        graph : ContextGraph
            The graph to search and mutate.
        new_node : Node
            The newly ingested node.
        max_candidates : int
            Maximum number of candidates to check.

        Returns
        -------
        list[ContradictionResult]
            All detected contradictions/supersessions (after filtering
            by confidence threshold).
        """
        if not self.enabled:
            return []

        candidates = self.find_candidates(
            graph, new_node, max_candidates=max_candidates
        )
        if not candidates:
            return []

        results = self.check_contradictions(new_node, candidates)

        # Apply only results above the confidence threshold.
        applied: List[ContradictionResult] = []
        for result in results:
            if result.confidence >= self.config.contradiction_confidence_threshold:
                edge = self.apply_supersession(
                    graph, new_node.id, result.existing_node_id, result
                )
                if edge is not None:
                    applied.append(result)

        return applied

    # -- Internal -----------------------------------------------------------

    def _check_pair(
        self, new_node: Node, existing_node: Node
    ) -> Optional[ContradictionResult]:
        """Check a single pair of nodes for contradiction/supersession."""
        assert self._llm is not None

        prompt = _USER_PROMPT_TEMPLATE.format(
            a_type=existing_node.node_type,
            a_content=existing_node.content,
            b_type=new_node.node_type,
            b_content=new_node.content,
        )

        try:
            raw_response = self._llm(prompt, system=_SYSTEM_PROMPT)
            parsed = self._parse_response(raw_response)
            return ContradictionResult(
                existing_node_id=existing_node.id,
                relation=parsed.get("relation", "unrelated"),
                confidence=float(parsed.get("confidence", 0.0)),
                evidence=parsed.get("evidence", ""),
            )
        except Exception:
            logger.warning(
                "Failed to check contradiction between %s and %s",
                new_node.id, existing_node.id,
                exc_info=True,
            )
            return None

    @staticmethod
    def _parse_response(raw: str) -> Dict[str, Any]:
        """Parse the LLM's JSON response, with fallback handling."""
        # Strip markdown code fences if present.
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (fences).
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        parsed = json.loads(text)

        # Validate relation value.
        valid_relations = {"supersedes", "contradicts", "supports", "unrelated"}
        if parsed.get("relation") not in valid_relations:
            parsed["relation"] = "unrelated"

        # Clamp confidence.
        conf = float(parsed.get("confidence", 0.0))
        parsed["confidence"] = max(0.0, min(1.0, conf))

        return parsed
