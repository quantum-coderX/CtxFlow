"""Skill Router — deterministic query classifier and execution planner.

Classifies user intent into explicit execution graphs:
- ``NEW_LITERATURE``: Retriever → Reader → Synthesizer → Critic
- ``EXISTING_KNOWLEDGE``: Synthesizer → Critic
- ``CLAIM_VERIFICATION``: Critic only
- ``EXPLORATORY_TRACE``: GraphQuery → Provenance Tracer

The router uses rule-based keyword classification (no LLM calls)
to select the minimal agent pipeline for each query.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from researchos.agents.base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class QueryIntent(str, Enum):
    """Classified intent of a user query."""

    NEW_LITERATURE = "new_literature"
    EXISTING_KNOWLEDGE = "existing_knowledge"
    CLAIM_VERIFICATION = "claim_verification"
    EXPLORATORY_TRACE = "exploratory_trace"
    UNKNOWN = "unknown"


# Keyword patterns for intent classification
_INTENT_PATTERNS: Dict[QueryIntent, List[str]] = {
    QueryIntent.NEW_LITERATURE: [
        r"\bfind\b.*\bpaper",
        r"\bsearch\b.*\bliterature",
        r"\blatest\b.*\bresearch",
        r"\bnew\b.*\bstud",
        r"\barxiv\b",
        r"\brecent\b.*\bwork",
        r"\bfetch\b",
        r"\bdownload\b.*\bpaper",
        r"\bwhat papers\b",
        r"\bsurvey\b.*\bliterature",
    ],
    QueryIntent.CLAIM_VERIFICATION: [
        r"\bverif",
        r"\bcheck\b.*\bclaim",
        r"\bvalidat",
        r"\bcontradiction",
        r"\bsupersed",
        r"\bis it true",
        r"\bfact.?check",
        r"\bconfirm\b",
    ],
    QueryIntent.EXPLORATORY_TRACE: [
        r"\btrace\b",
        r"\bprovenance\b",
        r"\blineage\b",
        r"\bwhere did\b.*\bcome from",
        r"\bsource\b.*\bclaim",
        r"\bcitation\b.*\bchain",
        r"\baudit\b",
        r"\bhow do we know\b",
    ],
    QueryIntent.EXISTING_KNOWLEDGE: [
        r"\bwhat\b.*\bknow",
        r"\bsummarize\b",
        r"\bexplain\b",
        r"\btell me about\b",
        r"\bwhat is\b",
        r"\bdescribe\b",
        r"\bcompare\b",
        r"\breview\b",
    ],
}


def classify_intent(query: str) -> QueryIntent:
    """Classify a query into a QueryIntent using keyword matching.

    Parameters
    ----------
    query : str
        The user's natural-language query.

    Returns
    -------
    QueryIntent
    """
    lower = query.lower()

    # Score each intent by number of matching patterns
    scores: Dict[QueryIntent, int] = {}
    for intent, patterns in _INTENT_PATTERNS.items():
        score = sum(1 for p in patterns if re.search(p, lower))
        if score > 0:
            scores[intent] = score

    if not scores:
        return QueryIntent.UNKNOWN

    # Return the intent with the highest score
    return max(scores, key=lambda k: scores[k])


# ---------------------------------------------------------------------------
# Execution Plans
# ---------------------------------------------------------------------------

# Maps intents to ordered lists of agent names to execute
_EXECUTION_PLANS: Dict[QueryIntent, List[str]] = {
    QueryIntent.NEW_LITERATURE: [
        "retriever", "reader", "synthesizer", "critic",
    ],
    QueryIntent.EXISTING_KNOWLEDGE: [
        "synthesizer", "critic",
    ],
    QueryIntent.CLAIM_VERIFICATION: [
        "critic",
    ],
    QueryIntent.EXPLORATORY_TRACE: [
        "graph_query",
    ],
    QueryIntent.UNKNOWN: [
        "synthesizer", "critic",
    ],
}


# ---------------------------------------------------------------------------
# SkillRouter
# ---------------------------------------------------------------------------

class SkillRouter(BaseAgent):
    """Deterministic query router that dispatches to agent pipelines.

    The router classifies user intent and builds an execution plan
    specifying which agents to invoke and in what order.

    Parameters
    ----------
    agents : dict[str, BaseAgent]
        Map of agent name → agent instance. The router invokes agents
        from this registry.
    """

    def __init__(
        self,
        agents: Optional[Dict[str, BaseAgent]] = None,
        llm: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        super().__init__(name="router", llm=llm)
        self._agents: Dict[str, BaseAgent] = agents or {}

    def register_agent(self, name: str, agent: BaseAgent) -> None:
        """Register an agent in the router's agent registry."""
        self._agents[name] = agent
        logger.info("Router registered agent: %s", name)

    def execute(self, **kwargs: Any) -> AgentResult:
        """Route a query through the appropriate agent pipeline.

        Parameters
        ----------
        query : str
            The user's natural-language query.
        context : dict | None
            Additional context passed to agents.

        Returns
        -------
        AgentResult
            Contains the final pipeline result and execution metadata.
        """
        query: str = kwargs.get("query", "")
        context: Dict[str, Any] = kwargs.get("context", {})

        if not query:
            return AgentResult(
                agent_name=self.name,
                success=False,
                error="No query provided.",
            )

        # 1. Classify intent
        intent = classify_intent(query)
        logger.info("Classified intent: %s for query: %s", intent.value, query[:100])

        # 2. Get execution plan
        plan = _EXECUTION_PLANS.get(intent, _EXECUTION_PLANS[QueryIntent.UNKNOWN])

        # 3. Execute pipeline
        pipeline_results: List[AgentResult] = []
        current_data: Any = {"query": query, **context}

        for agent_name in plan:
            agent = self._agents.get(agent_name)
            if agent is None:
                logger.warning(
                    "Agent '%s' not registered, skipping", agent_name
                )
                continue

            result = agent.run(
                query=query,
                context=current_data,
                previous_results=pipeline_results,
            )
            pipeline_results.append(result)

            if not result.success:
                logger.warning(
                    "Agent '%s' failed, continuing pipeline: %s",
                    agent_name, result.error,
                )
                continue

            # Pass output forward as context for the next agent
            if result.data is not None:
                current_data = {
                    "query": query,
                    "previous_output": result.data,
                    **context,
                }

        # 4. Build final result
        final_data = pipeline_results[-1].data if pipeline_results else None
        all_success = all(r.success for r in pipeline_results)
        failed_agents = [r.agent_name for r in pipeline_results if not r.success]

        # Hoist the retriever's external-search outcome so API/SSE consumers can
        # see a provider outage without digging into per-agent payloads.
        external_search = None
        for previous in pipeline_results:
            if previous.agent_name == "retriever" and isinstance(previous.data, dict):
                external_search = previous.data.get("external_search")
                break

        metadata: Dict[str, Any] = {
            "intent": intent.value,
            "plan": plan,
            "agents_executed": len(pipeline_results),
            "partial": not all_success,
        }
        if failed_agents:
            metadata["failed_agents"] = failed_agents
        if isinstance(external_search, dict):
            metadata["external_search_status"] = external_search.get("status")

        # Hoist the synthesizer's context-window accounting so the API/UI can
        # show how many retrieved nodes made it into the answer.
        for previous in pipeline_results:
            if previous.agent_name == "synthesizer" and isinstance(
                previous.metadata, dict
            ):
                usage = {
                    key: previous.metadata[key]
                    for key in (
                        "nodes_retrieved",
                        "nodes_used",
                        "nodes_dropped",
                        "tokens_used",
                        "token_budget",
                    )
                    if key in previous.metadata
                }
                if usage:
                    metadata["context_usage"] = usage
                break

        return AgentResult(
            agent_name=self.name,
            success=bool(pipeline_results) and all_success,
            data={
                "intent": intent.value,
                "plan": plan,
                "result": final_data,
                "pipeline_results": [
                    {
                        "agent": r.agent_name,
                        "success": r.success,
                        "duration_ms": r.duration_ms,
                        "error": r.error,
                    }
                    for r in pipeline_results
                ],
                "external_search": external_search,
            },
            metadata=metadata,
        )
