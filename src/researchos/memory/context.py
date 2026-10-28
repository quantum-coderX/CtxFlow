"""Optional adapter from the durable CtxFlow memory store to ResearchOS."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol

from ctxflow.memory.store import MemoryStore
from ctxflow.memory.tokens import SimpleTokenizer, count_tokens

_MEMORY_ENABLED_ENV = "RESEARCHOS_MEMORY_ENABLED"


@dataclass(frozen=True)
class ContextRecord:
    memory_id: str
    text: str
    tokens: int
    memory_type: str
    validity: str
    score: float
    source_type: str | None = None
    source_id: str | None = None
    superseded_by: str | None = None


class ContextProvider(Protocol):
    async def retrieve(
        self, project_id: str, query: str, token_budget: int
    ) -> list[ContextRecord]:
        ...


def memory_enabled(value: str | None = None) -> bool:
    """Return whether durable memory integration is explicitly enabled."""

    raw_value = os.getenv(_MEMORY_ENABLED_ENV) if value is None else value
    if raw_value is None or not raw_value.strip():
        return False
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{_MEMORY_ENABLED_ENV} must be one of "
        "1, 0, true, false, yes, no, on, or off"
    )


class MemoryContextProvider:
    """Token-budgeted, project-scoped retrieval over a MemoryStore."""

    def __init__(self, store: MemoryStore, *, candidate_limit: int = 100) -> None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        self.store = store
        self.candidate_limit = candidate_limit
        self.tokenizer = SimpleTokenizer()

    async def retrieve(
        self, project_id: str, query: str, token_budget: int
    ) -> list[ContextRecord]:
        if not project_id.strip():
            raise ValueError("project_id is required")
        if token_budget < 0:
            raise ValueError("token_budget must be non-negative")
        scored = await self.store.search(
            None,
            query,
            self.candidate_limit,
            project_id=project_id,
            include_history=False,
        )
        selected: list[ContextRecord] = []
        used = 0
        for item in scored:
            memory = await self.store.get(item.memory_id, project_id=project_id)
            if memory is None:
                continue
            tokens = count_tokens(memory, self.tokenizer)
            provenance = None
            get_provenance = getattr(self.store, "get_provenance", None)
            if get_provenance is not None:
                provenance = await get_provenance(
                    memory.id, project_id=project_id
                )
            if used + tokens > token_budget:
                continue
            selected.append(
                ContextRecord(
                    memory_id=memory.id,
                    text=memory.text,
                    tokens=tokens,
                    memory_type=memory.memory_type.value,
                    validity=memory.validity.value,
                    score=item.score,
                    source_type=(
                        provenance.source_type.value if provenance is not None else None
                    ),
                    source_id=provenance.source_id if provenance is not None else None,
                )
            )
            used += tokens
        return selected


def create_context_provider(
    store: MemoryStore,
    *,
    candidate_limit: int = 100,
    enabled: str | None = None,
) -> MemoryContextProvider | None:
    """Create the adapter only when durable memory integration is enabled."""

    if not memory_enabled(enabled):
        return None
    return MemoryContextProvider(store, candidate_limit=candidate_limit)
