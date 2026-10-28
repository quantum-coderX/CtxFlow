"""Critic Agent — contradiction and truth validation.

The Critic validates claims in a synthesized answer against the CtxFlow
knowledge graph's lineage and contradiction edges. It checks:
1. Whether cited claims have been superseded.
2. Whether any contradictions exist for referenced nodes.
3. Whether provenance chains are valid and traceable.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ctxflow import CtxFlow
from ctxflow.models import NodeStatus, ResearchTrace
from researchos.agents.base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

class CriticAgent(BaseAgent):
    """Agent that validates claims against graph lineage and contradictions.

    Parameters
    ----------
    ctxflow : CtxFlow
        The CtxFlow instance for provenance traversal.
    llm : callable | None
        LLM callable for deeper semantic validation.
    """

    def __init__(
        self,
        ctxflow: CtxFlow,
        llm: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        super().__init__(name="critic", llm=llm)
        self._ctx = ctxflow

    def execute(self, **kwargs: Any) -> AgentResult:
        """Validate an answer against the knowledge graph.

        Parameters
        ----------
        query : str
            The original user query.
        context : dict
            Pipeline context containing previous_output from Synthesizer.

        Returns
        -------
        AgentResult
            Contains validation verdict, warnings, and provenance data.
        """
        query: str = kwargs.get("query", "")
        context: Dict[str, Any] = kwargs.get("context", {})
        previous_results: List[AgentResult] = kwargs.get("previous_results", [])

        # Get the synthesized answer and cited nodes
        cited_nodes: List[str] = []
        answer: str = ""
        alerts: List[Dict[str, Any]] = []

        for prev in previous_results:
            if prev.agent_name == "synthesizer" and prev.data:
                cited_nodes = prev.data.get("cited_nodes", [])
                answer = prev.data.get("answer", "")
                alerts = prev.data.get("alerts", [])
                break

        # Also check context
        prev_output = context.get("previous_output", {})
        if isinstance(prev_output, dict):
            if not cited_nodes:
                cited_nodes = prev_output.get("cited_nodes", [])
            if not answer:
                answer = prev_output.get("answer", "")

        # CLAIM_VERIFICATION routes directly to the Critic, so there may be
        # no synthesized draft. Fall back to verifying the query itself.
        if not cited_nodes and query:
            verification = self.verify_claim(query)
            supporting = verification.get("supporting_evidence", [])
            contradicted = verification.get("contradicting_evidence", [])
            superseded_evidence = verification.get("superseded_evidence", [])

            cited_nodes = (
                [item["node_id"] for item in supporting]
                + [item["node_id"] for item in contradicted]
                + [item["node_id"] for item in superseded_evidence]
            )
            answer = (
                f"Verification of {query!r}: "
                f"{len(supporting)} supporting claim(s), "
                f"{len(contradicted)} contradicting claim(s), and "
                f"{len(superseded_evidence)} superseded claim(s) found."
            )
            alerts = [
                {
                    "type": "superseded",
                    "node_id": item["node_id"],
                    "superseded_by": item.get("superseded_by"),
                    "message": (
                        f"Node [{item['node_id']}] has been superseded."
                    ),
                }
                for item in superseded_evidence
            ]

        # Validate each cited node
        validations: List[Dict[str, Any]] = []
        supersession_warnings: List[Dict[str, Any]] = []
        contradiction_warnings: List[Dict[str, Any]] = []

        packed_ids = context.get("packed_node_ids") or []
        packed_set = set(packed_ids) if packed_ids else None

        for node_id in cited_nodes:
            validation = self._validate_node(node_id)
            if packed_set is not None and node_id not in packed_set:
                validation["valid"] = False
                validation["outside_packed_context"] = True
                alerts = alerts + [
                    {
                        "type": "invalid_citation",
                        "node_id": node_id,
                        "message": (
                            f"Cited node [{node_id}] was not supplied in the "
                            "packed evidence context."
                        ),
                    }
                ]
            validations.append(validation)

            if validation.get("has_supersession"):
                supersession_warnings.extend(validation.get("supersessions", []))
            if validation.get("has_contradictions"):
                contradiction_warnings.extend(validation.get("contradictions", []))

        # Build verdict
        invalid_citations = [
            item for item in validations
            if not item.get("exists", True) or item.get("outside_packed_context")
        ]
        has_evidence = bool(cited_nodes)
        is_verified = (
            has_evidence
            and not invalid_citations
            and len(supersession_warnings) == 0
            and len(contradiction_warnings) == 0
        )
        if not has_evidence:
            # Nothing was checked: "no warnings" is not the same as "verified".
            verdict = "INSUFFICIENT_EVIDENCE"
            alerts = alerts + [
                {
                    "type": "insufficient_evidence",
                    "message": (
                        "No claim, node, or supporting evidence was available "
                        "to verify; the answer is unverified."
                    ),
                }
            ]
        elif is_verified:
            verdict = "VERIFIED"
        else:
            verdict = "WARNINGS_FOUND"

        return AgentResult(
            agent_name=self.name,
            success=True,
            data={
                "verdict": verdict,
                "verified": is_verified,
                "answer": answer,
                "cited_nodes": list(cited_nodes),
                "validations": validations,
                "supersession_warnings": supersession_warnings,
                "contradiction_warnings": contradiction_warnings,
                "total_claims_checked": len(cited_nodes),
                "alerts": alerts + supersession_warnings + contradiction_warnings,
            },
            metadata={
                "verdict": verdict,
                "claims_checked": len(cited_nodes),
                "supersessions_found": len(supersession_warnings),
                "contradictions_found": len(contradiction_warnings),
            },
        )

    def _validate_node(self, node_id: str) -> Dict[str, Any]:
        """Validate a single node against the knowledge graph."""
        node = self._ctx.graph.get_node(node_id)
        if node is None:
            return {
                "node_id": node_id,
                "exists": False,
                "valid": False,
                "has_supersession": False,
                "has_contradictions": False,
            }

        # Check node status
        is_superseded = node.status in (NodeStatus.SUPERSEDED, "superseded")
        is_contested = node.status in (NodeStatus.CONTESTED, "contested")

        # Get research trace for provenance
        trace: ResearchTrace = self._ctx.research_trace(node_id)

        # Collect supersessions
        supersessions: List[Dict[str, Any]] = []
        if is_superseded:
            supersessions.append({
                "type": "superseded",
                "node_id": node_id,
                "superseded_by": node.superseded_by,
                "message": (
                    f"Node [{node_id}] has been superseded by "
                    f"[{node.superseded_by}]."
                ),
            })

        for sup_node in trace.superseded_claims:
            if sup_node.id != node_id:
                supersessions.append({
                    "type": "chain_supersession",
                    "node_id": sup_node.id,
                    "superseded_by": sup_node.superseded_by,
                    "message": (
                        f"Source node [{sup_node.id}] in the provenance chain "
                        f"has been superseded."
                    ),
                })

        # Collect contradictions
        contradictions: List[Dict[str, Any]] = []
        for contra_node, contra_edge in trace.contradicting_claims:
            contradictions.append({
                "type": "contradiction",
                "node_id": contra_node.id,
                "relation": contra_edge.relation_type,
                "confidence": contra_edge.metadata.get("confidence", 0.0),
                "evidence": contra_edge.metadata.get("evidence", ""),
                "message": (
                    f"Node [{contra_node.id}] {contra_edge.relation_type} "
                    f"claims in the provenance chain."
                ),
            })

        # Check provenance completeness
        has_sources = len(trace.source_papers) > 0
        chain_length = len(trace.supporting_chain.steps)

        return {
            "node_id": node_id,
            "exists": True,
            "valid": not is_superseded,
            "status": node.status if isinstance(node.status, str) else node.status.value,
            "has_supersession": len(supersessions) > 0,
            "has_contradictions": len(contradictions) > 0,
            "supersessions": supersessions,
            "contradictions": contradictions,
            "has_source_papers": has_sources,
            "provenance_chain_length": chain_length,
            "source_paper_count": len(trace.source_papers),
        }

    def verify_claim(
        self, claim_text: str, node_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Standalone claim verification against the knowledge graph.

        Parameters
        ----------
        claim_text : str
            The claim to verify.
        node_id : str | None
            Optional node ID to trace.

        Returns
        -------
        dict
            Verification result with evidence and confidence.
        """
        # Query for related nodes
        results = self._ctx.query(
            context=claim_text,
            k=10,
            include_superseded=True,
        )

        supporting = []
        contradicting = []
        superseded = []

        for r in results:
            if r.node.status in (NodeStatus.SUPERSEDED, "superseded"):
                superseded.append({
                    "node_id": r.node.id,
                    "content": r.node.content[:200],
                    "superseded_by": r.node.superseded_by,
                })
            elif r.score > 0.5:
                # Check for contradiction edges
                contra_edges = self._ctx.graph.get_edges_by_type(
                    r.node.id, "contradicts"
                ) + self._ctx.graph.get_edges_by_type(
                    r.node.id, "supersedes"
                )
                if contra_edges:
                    contradicting.append({
                        "node_id": r.node.id,
                        "content": r.node.content[:200],
                        "score": r.score,
                    })
                else:
                    supporting.append({
                        "node_id": r.node.id,
                        "content": r.node.content[:200],
                        "score": r.score,
                    })

        # Build trace if node_id provided
        trace_data = None
        if node_id:
            validation = self._validate_node(node_id)
            trace_data = validation

        return {
            "claim": claim_text[:500],
            "supporting_evidence": supporting,
            "contradicting_evidence": contradicting,
            "superseded_evidence": superseded,
            "confidence": (
                len(supporting) / max(len(supporting) + len(contradicting), 1)
            ),
            "trace": trace_data,
        }
