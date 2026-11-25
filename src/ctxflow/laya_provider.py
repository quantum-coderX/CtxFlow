"""Laya-enhanced context provider with learned relevance decisions.

Builds on LayaContextSelector to provide a ContextProvider that uses
System-1 decision reasoning instead of greedy token-packing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ctxflow.laya_selector import LayaContextSelector
from ctxflow.memory.store import MemoryStore
from ctxflow.memory.tokens import SimpleTokenizer


@dataclass(frozen=True)
class ContextRecord:
    """Single selected memory for the LLM context."""

    memory_id: str
    text: str
    tokens: int
    memory_type: str
    validity: str
    score: float
    reason: str  # Why Laya kept this: task_match, continuity, importance, etc.
    source_type: str | None = None
    source_id: str | None = None


class ContextProvider(Protocol):
    """Standard context provider interface."""

    async def retrieve(
        self, project_id: str, query: str, token_budget: int
    ) -> list[ContextRecord]:
        ...


class LayaMemoryContextProvider:
    """ContextProvider using Laya for learned context selection.

    Replaces greedy token-packing (MemoryContextProvider) with:
    - Hybrid search over embeddings + text
    - Laya's System-1 decision model (non-autoregressive)
    - Calibrated confidence scores (0-1, no hallucination)
    - Task-aware ranking (task dependencies, skills, session continuity)
    """

    def __init__(
        self,
        store: MemoryStore,
        selector: LayaContextSelector | None = None,
        candidate_limit: int = 200,
    ) -> None:
        """
        Parameters
        ----------
        store : MemoryStore
            Async memory storage for retrieval.
        selector : LayaContextSelector | None
            Laya selector instance. If None, creates one with defaults.
        candidate_limit : int
            Maximum memories to retrieve before Laya selection.
        """
        self.store = store
        self.candidate_limit = candidate_limit
        self.selector = selector or LayaContextSelector(
            store, candidate_limit=candidate_limit
        )

    @property
    def selector_name(self) -> str:
        return f"laya:{getattr(self.selector, 'router_name', 'laya')}"

    async def retrieve(
        self,
        project_id: str,
        query: str,
        token_budget: int,
        task_description: str = "general_query",
        required_skills: list[str] | None = None,
        previous_context_ids: list[str] | None = None,
    ) -> list[ContextRecord]:
        """
        Retrieve context using Laya's learned relevance decisions.

        Parameters
        ----------
        project_id : str
            Project scope for retrieval.
        query : str
            User query or task.
        token_budget : int
            Maximum tokens for the context.
        task_description : str
            High-level task (e.g., "tool_call: arxiv_search").
        required_skills : list[str] | None
            Skills/tools the LLM might call.
        previous_context_ids : list[str] | None
            Memory IDs from the previous turn.

        Returns
        -------
        list[ContextRecord]
            Selected memories with scores and reasons.
        """
        if not project_id.strip():
            raise ValueError("project_id is required")
        if token_budget < 0:
            raise ValueError("token_budget must be non-negative")

        # Use Laya selector to pick high-value memories.
        selected = await self.selector.select_context(
            project_id=project_id,
            query=query,
            task_description=task_description,
            required_skills=required_skills,
            previous_context_ids=previous_context_ids,
            token_budget=token_budget,
        )

        # Convert to ContextRecord format.
        records = [
            ContextRecord(
                memory_id=item["memory_id"],
                text=item["text"],
                tokens=item["tokens"],
                memory_type=item["memory_type"],
                validity=item["validity"],
                score=item["score"],
                reason=item["reason"],
                source_type=item.get("source_type"),
                source_id=item.get("source_id"),
            )
            for item in selected
        ]
        return records


def create_laya_context_provider(
    store: MemoryStore,
    *,
    candidate_limit: int = 200,
    enabled: str | None = None,
) -> LayaMemoryContextProvider | None:
    """Create the Laya provider only when durable memory integration is enabled."""

    from ctxflow.memory.context import memory_enabled

    if not memory_enabled(enabled):
        return None
    return LayaMemoryContextProvider(store, candidate_limit=candidate_limit)
