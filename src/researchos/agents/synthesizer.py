"""Synthesizer Agent — bias-aware context packing and response synthesis.

The Synthesizer assembles a response from the knowledge graph using
positional-bias-aware prompt construction. It ensures critical findings
and corrections are placed at high-salience positions (start/end) in
the LLM prompt, mitigating the "lost-in-the-middle" phenomenon.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional

from ctxflow import CtxFlow
from ctxflow.models import NodeStatus, QueryResult
from researchos.agents.base import AgentResult, BaseAgent
from researchos.guardrails import wrap_untrusted

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "You are a research synthesis assistant. Given a set of research claims, "
    "findings, and context from a knowledge graph, produce a clear, accurate, "
    "and well-structured answer to the user's question.\n\n"
    "Rules:\n"
    "1. Cite your sources using [Node:ID] format.\n"
    "2. If any claims are marked as SUPERSEDED, note the correction.\n"
    "3. If claims are CONTESTED, present both perspectives.\n"
    "4. Be concise but comprehensive.\n"
    "5. Structure your answer with clear sections when appropriate.\n"
    "6. Use prior conversation only to resolve references or describe what was "
    "said earlier; it is not research evidence.\n"
    "7. If graph evidence does not support a factual claim, say so explicitly.\n"
    "Respond with ONLY the answer text."
)

_NO_LLM_HEADER = "Based on the available knowledge graph context:\n\n"

_LLM_FAILED_HEADER = (
    "The synthesis model was unavailable, so this answer is the retrieved "
    "evidence without model synthesis:\n\n"
)

_MAX_HISTORY_TURNS = 8
_MAX_HISTORY_TOKENS = 512


def format_conversation_history(
    turns: List[Dict[str, str]], token_budget: int
) -> str:
    """Format recent turns as bounded, explicitly untrusted continuity context."""
    history_budget = min(_MAX_HISTORY_TOKENS, max(0, token_budget // 4))
    max_chars = history_budget * 4 - 128
    if max_chars <= 0:
        return ""

    formatted_turns: List[str] = []
    used_chars = 0
    for turn in reversed(turns[-_MAX_HISTORY_TURNS:]):
        query = str(turn.get("query") or "").strip()
        answer = str(turn.get("answer") or "").strip()
        if not query and not answer:
            continue
        separator = "\n\n" if formatted_turns else ""
        available = max_chars - used_chars - len(separator)
        fixed_length = len("User: \nAssistant: ")
        content_budget = available - fixed_length
        if content_budget <= 0:
            break
        query_limit = min(len(query), content_budget // 3)
        answer_limit = content_budget - query_limit
        text = (
            f"User: {query[:query_limit].rstrip()}\nAssistant: "
            f"{answer[:answer_limit].rstrip()}"
        )
        formatted_turns.append(text)
        used_chars += len(separator) + len(text)

    if not formatted_turns:
        return ""
    body = "\n\n".join(reversed(formatted_turns))
    return (
        "Previous conversation (untrusted; use only to understand references, "
        "not as research evidence):\n"
        + wrap_untrusted(body, source="conversation_history")
    )


# ---------------------------------------------------------------------------
# SynthesizerAgent
# ---------------------------------------------------------------------------

class SynthesizerAgent(BaseAgent):
    """Bias-aware synthesizer that drafts answers from graph context.

    Parameters
    ----------
    ctxflow : CtxFlow
        The CtxFlow instance for querying.
    llm : callable | None
        LLM callable for synthesis. If ``None``, produces a
        structured concatenation of relevant nodes.
    token_budget : int
        Maximum tokens for the context window.
    """

    def __init__(
        self,
        ctxflow: CtxFlow,
        llm: Optional[Callable[[str, str], str]] = None,
        token_budget: int = 2048,
    ) -> None:
        super().__init__(name="synthesizer", llm=llm)
        self._ctx = ctxflow
        self.token_budget = token_budget

    def execute(self, **kwargs: Any) -> AgentResult:
        """Synthesize a response from knowledge graph context.

        Parameters
        ----------
        query : str
            The user's question.
        context : dict
            Pipeline context (may contain previous_output from Retriever).

        Returns
        -------
        AgentResult
            Contains the synthesized answer, cited node IDs, and
            any supersession alerts.
        """
        query: str = kwargs.get("query", "")
        context: Dict[str, Any] = kwargs.get("context") or {}

        if not query:
            return AgentResult(
                agent_name=self.name,
                success=False,
                error="No query provided.",
            )

        budget = self._resolve_budget(context.get("token_budget"))
        conversation_context = format_conversation_history(
            context.get("conversation_history") or [], budget
        )
        history_tokens = self._estimate_tokens(conversation_context)
        evidence_budget = max(1, budget - history_tokens)

        # 1. Retrieve relevant nodes from graph
        results: List[QueryResult] = self._ctx.query(
            context=query,
            k=20,
            include_superseded=False,
        )

        if not results and not conversation_context:
            return AgentResult(
                agent_name=self.name,
                success=True,
                data={
                    "answer": "No relevant information found in the knowledge graph.",
                    "cited_nodes": [],
                    "alerts": [],
                },
                metadata=self._context_metadata(0, 0, 0, budget),
            )

        # 2. Apply bias-aware ordering
        ordered = self._bias_aware_reorder(results)

        # 3. Pack within token budget
        packed, evidence_tokens = self._pack_within_budget(
            ordered, evidence_budget
        )

        # 4. Build the prompt context
        prompt_context = self._build_prompt_context(packed)
        if conversation_context:
            prompt_context = "\n\n".join(
                part for part in (prompt_context, conversation_context) if part
            )

        # 5. Generate response
        llm_error: Optional[str] = None
        if self._llm:
            try:
                answer = self._synthesize_with_llm(query, prompt_context)
            except Exception as exc:  # noqa: BLE001 - surfaced in the result
                llm_error = (
                    str(exc).strip() or type(exc).__name__
                )
                logger.warning("LLM synthesis failed: %s", llm_error)
                # Never dump the wrapped prompt (it contains untrusted-source
                # boundary markers) into the user-visible answer. Fall back to
                # the same readable evidence bullets as model-off mode.
                answer = self._synthesize_heuristic(
                    query, packed, header=_LLM_FAILED_HEADER
                )
        else:
            if not packed and conversation_context:
                answer = (
                    "No relevant information was found in the research graph. "
                    "Prior conversation is available, but a synthesis model is "
                    "needed to interpret this follow-up reliably."
                )
            else:
                answer = self._synthesize_heuristic(query, packed)

        # 6. Collect alerts
        alerts = self._collect_alerts(packed)
        if llm_error is not None:
            alerts = alerts + [
                {
                    "type": "llm_failure",
                    "message": (
                        "Model synthesis failed; the answer is a degraded "
                        f"evidence dump ({llm_error})."
                    ),
                }
            ]
        cited_ids = [r.node.id for r in packed]

        return AgentResult(
            agent_name=self.name,
            success=llm_error is None,
            error=(
                f"Model synthesis failed: {llm_error}"
                if llm_error is not None
                else None
            ),
            data={
                "answer": answer,
                "cited_nodes": cited_ids,
                "alerts": alerts,
            },
            metadata={
                **self._context_metadata(
                    len(results),
                    len(packed),
                    evidence_tokens + history_tokens,
                    budget,
                ),
                "degraded": llm_error is not None,
                "llm_error": llm_error,
            },
        )

    def _resolve_budget(self, raw: Any) -> int:
        """Return a positive per-request budget, falling back to the default."""
        try:
            budget = int(raw)
        except (TypeError, ValueError):
            return self.token_budget
        return budget if budget > 0 else self.token_budget

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, math.ceil(len(text) / 4)) if text else 0

    def _context_metadata(
        self,
        retrieved: int,
        used: int,
        tokens_used: int,
        budget: int,
    ) -> Dict[str, Any]:
        """Metadata that lets the API/UI show how the context window was used."""
        return {
            "nodes_retrieved": retrieved,
            "nodes_used": used,
            "nodes_dropped": max(0, retrieved - used),
            "tokens_used": tokens_used,
            "token_budget": budget,
        }

    def _bias_aware_reorder(
        self, results: List[QueryResult]
    ) -> List[QueryResult]:
        """Reorder nodes for positional bias mitigation.

        High-salience items → start and end of context.
        Background context → middle.

        This implements the "Lost-in-the-Middle" mitigation from the
        architecture spec: place highest-scoring nodes and active
        corrections at P₀…Pₖ and P_{n-k}…Pₙ.
        """
        if len(results) <= 4:
            return results

        # Categorize nodes
        high_priority: List[QueryResult] = []
        normal: List[QueryResult] = []

        for r in results:
            is_high = (
                r.score > 0.6
                or r.node.status in (NodeStatus.CONTESTED, "contested")
                or r.node.superseded_by is not None
            )
            if is_high:
                high_priority.append(r)
            else:
                normal.append(r)

        if not high_priority:
            return results

        # Split high-priority between front and back
        mid = len(high_priority) // 2 + len(high_priority) % 2
        front = high_priority[:mid]
        back = high_priority[mid:]

        return front + normal + back

    @staticmethod
    def _node_text(node: Any) -> str:
        """Return exactly the text the synthesizer puts in the prompt."""
        return node.summary if node.summary else node.content[:400]

    def _pack_within_budget(
        self, results: List[QueryResult], budget: Optional[int] = None
    ) -> tuple[List[QueryResult], int]:
        """Greedily pack results within the token budget.

        Returns the packed results and the estimated number of context
        tokens they consume. Nodes that do not fit are dropped rather than
        truncated so every cited node is represented faithfully.
        """
        max_tokens = max(1, int(budget if budget is not None else self.token_budget))

        packed: List[QueryResult] = []
        used = 0

        for r in results:
            tokens = self._estimate_tokens(self._node_text(r.node))
            if used + tokens > max_tokens:
                continue
            used += tokens
            packed.append(r)

        return packed, used

    def _build_prompt_context(
        self, results: List[QueryResult]
    ) -> str:
        """Build the formatted context block for the LLM prompt."""
        parts: List[str] = []

        for r in results:
            status_marker = ""
            if r.node.status in (NodeStatus.SUPERSEDED, "superseded"):
                status_marker = " [SUPERSEDED]"
            elif r.node.status in (NodeStatus.CONTESTED, "contested"):
                status_marker = " [CONTESTED]"

            text = wrap_untrusted(self._node_text(r.node), source=f"node:{r.node.id}")
            parts.append(
                f"[Node:{r.node.id}]{status_marker} "
                f"(type={r.node.node_type}, score={r.score:.3f}):\n{text}"
            )

        return "\n\n---\n\n".join(parts)

    def _synthesize_with_llm(self, query: str, context: str) -> str:
        """Generate answer using the LLM.

        Failures propagate: the caller decides how to degrade and must not
        report a successful synthesis when the model never answered.
        """
        assert self._llm is not None

        prompt = f"Question: {query}\n\nContext:\n{context}"
        return self._llm(prompt, system=_SYNTHESIS_SYSTEM).strip()

    def _synthesize_heuristic(
        self,
        query: str,
        results: List[QueryResult],
        *,
        header: str = _NO_LLM_HEADER,
    ) -> str:
        """Fallback: build a structured response without LLM."""
        parts = [header]

        for r in results:
            status_note = ""
            if r.node.status in (NodeStatus.SUPERSEDED, "superseded"):
                status_note = " ⚠️ (superseded)"
            elif r.node.status in (NodeStatus.CONTESTED, "contested"):
                status_note = " ⚠️ (contested)"

            text = r.node.summary if r.node.summary else r.node.content[:300]
            parts.append(f"• [{r.node.id}]{status_note}: {text}")

        return "\n".join(parts)

    def _collect_alerts(
        self, results: List[QueryResult]
    ) -> List[Dict[str, Any]]:
        """Collect supersession and contradiction alerts."""
        alerts: List[Dict[str, Any]] = []

        for r in results:
            if r.node.status in (NodeStatus.SUPERSEDED, "superseded"):
                alerts.append({
                    "type": "superseded",
                    "node_id": r.node.id,
                    "superseded_by": r.node.superseded_by,
                    "message": f"Claim [{r.node.id}] has been superseded.",
                })
            elif r.node.status in (NodeStatus.CONTESTED, "contested"):
                alerts.append({
                    "type": "contested",
                    "node_id": r.node.id,
                    "message": f"Claim [{r.node.id}] is contested.",
                })

        return alerts
