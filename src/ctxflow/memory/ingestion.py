"""Rule-based, idempotent asynchronous memory ingestion."""

from __future__ import annotations

import hashlib
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from ctxflow.memory.classification import classify_memory
from ctxflow.memory.deduplication import (
    ANN_CANDIDATE_LIMIT,
    DEFAULT_DUPLICATE_THRESHOLD,
    DuplicateMatch,
    duplicate_edge,
    find_duplicates,
)
from ctxflow.memory.edge_proposer import LLMEdgeProposer
from ctxflow.memory.embeddings import Embedder, embed_texts
from ctxflow.memory.importance import ImportanceWeights, score_importance
from ctxflow.memory.store import (
    Edge,
    Memory,
    MemoryStore,
    MemoryType,
    MemoryTier,
    Provenance,
    Relation,
    SourceType,
)
from ctxflow.memory.tokens import SimpleTokenizer, TokenizerAdapter, count_tokens

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    event_id: str
    session_id: str
    ts: datetime
    role: str
    content: str
    project_id: str = "default"
    tool_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestResult:
    """Outcome of core ingestion and optional edge enrichment.

    A failed enrichment is observable through ``edge_enrichment_failed``.
    Because the event remains idempotently committed, a later worker can load
    the returned memory IDs and retry enrichment without replaying ingestion.
    """

    event_hash: str
    memory_ids: list[str]
    duplicate: bool = False
    edge_enrichment_failed: bool = False


class EventIngestor:
    """Ingestion pipeline for extraction, enrichment, linking, and persistence."""

    def __init__(
        self,
        store: MemoryStore,
        embedder: Embedder,
        extractor: Any | None = None,
        *,
        tokenizer: TokenizerAdapter | None = None,
        edge_proposer: LLMEdgeProposer | None = None,
        duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
        importance_weights: ImportanceWeights | None = None,
        provider_timeout: float = 30.0,
        provider_retries: int = 2,
    ) -> None:
        if not 0 <= duplicate_threshold <= 1:
            raise ValueError("duplicate_threshold must be between 0 and 1")
        self.store = store
        self.embedder = embedder
        self.extractor = extractor
        self.tokenizer = tokenizer or SimpleTokenizer()
        self.edge_proposer = edge_proposer
        self.duplicate_threshold = duplicate_threshold
        self.importance_weights = importance_weights
        if provider_timeout <= 0:
            raise ValueError("provider_timeout must be positive")
        if provider_retries < 0:
            raise ValueError("provider_retries must be >= 0")
        self.provider_timeout = provider_timeout
        self.provider_retries = provider_retries
        self._seen: dict[str, list[str]] = {}
        self._last_by_session: dict[str, str] = {}
        self._events: dict[str, Event] = {}
        self._known_memories: dict[str, Memory] = {}
        self._edge_enrichment_failures: dict[str, bool] = {}

    async def ingest(self, event: Event) -> IngestResult:
        hash_input = (
            event.event_id
            if event.project_id == "default"
            else f"{event.project_id}:{event.event_id}"
        )
        event_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        if event_hash in self._seen:
            return IngestResult(
                event_hash,
                list(self._seen[event_hash]),
                duplicate=True,
                edge_enrichment_failed=self._edge_enrichment_failures.get(event_hash, False),
            )

        atomic = await self._extract(event, event_hash)
        embeddings = await embed_texts(self.embedder, [item.text for item in atomic])
        memories: list[Memory] = []
        for index, (item, embedding) in enumerate(zip(atomic, embeddings)):
            memory_id = hashlib.sha256(
                f"{event_hash}:{index}:{event.role}:{item.text}".encode("utf-8")
            ).hexdigest()[:16]
            memories.append(Memory(
                id=memory_id,
                text=item.text,
                memory_type=self._memory_type(item.type, event, item.text),
                tier=MemoryTier.EPISODIC,
                created_at=event.ts,
                session_id=event.session_id,
                project_id=event.project_id,
                source=event.tool_name or event.role,
                embedding_model=self.embedder.model,
                embedding=embedding,
                entities=list(item.entities),
            ))
            count_tokens(memories[-1], self.tokenizer)
        parent = self._last_by_session.get(event.session_id)
        previous_ids = [parent] + [memory.id for memory in memories[:-1]]
        edges = [
            Edge(memory.id, previous_id, Relation.FOLLOWS)
            for previous_id, memory in zip(previous_ids, memories)
            if previous_id is not None
        ]
        edge_candidates: list[tuple[Memory, list[Memory]]] = []
        canonical_matches: list[DuplicateMatch | None] = []
        for memory in memories:
            duplicate_matches = await self._find_duplicate_matches(memory)
            canonical = duplicate_matches[0] if duplicate_matches else None
            canonical_matches.append(canonical)
            if canonical is not None:
                edges.append(Edge(*duplicate_edge(memory.id, canonical)))
            if self.edge_proposer is not None:
                edge_candidates.append((memory, await self._retrieval_candidates(memory)))
        for memory in memories:
            score_importance(
                memory,
                task_relevance=1.0 if event.tool_name else 0.5,
                dependent_count=sum(edge.src == memory.id for edge in edges),
                max_dependent_count=max(1, len(edges)),
                novelty=1.0,
                weights=self.importance_weights,
            )
        provenances = [
            Provenance(
                memory_id=memory.id,
                source_type=SourceType.TOOL_OUTPUT if event.tool_name else SourceType.USER_MESSAGE,
                source_id=event.event_id,
                tool=event.tool_name,
            )
            for memory in memories
        ]
        memory_ids = [memory.id for memory in memories]
        persist_many = getattr(self.store, "persist_ingestions", None)
        persist_ingestion = getattr(self.store, "persist_ingestion", None)
        if persist_many is not None:
            persisted = await persist_many(event_hash, event.event_id, memories, edges, provenances)
            if not persisted:
                return IngestResult(event_hash, memory_ids, duplicate=True)
        elif persist_ingestion is not None and len(memories) == 1:
            persisted = await persist_ingestion(
                event_hash, event.event_id, memories[0], edges[0] if edges else None, provenances[0]
            )
            if not persisted:
                return IngestResult(event_hash, memory_ids, duplicate=True)
        else:
            claim_event = getattr(self.store, "claim_event", None)
            if claim_event is not None and not await claim_event(event_hash, event.event_id):
                return IngestResult(event_hash, memory_ids, duplicate=True)
            for memory in memories:
                await self.store.add(memory)
            for edge in edges:
                await self._add_edge(edge)
            for provenance in provenances:
                await self._add_provenance(provenance)
        self._seen[event_hash] = memory_ids
        self._events[event_hash] = event
        self._known_memories.update((memory.id, memory) for memory in memories)
        self._last_by_session[event.session_id] = memories[-1].id
        for match in canonical_matches:
            if match is not None:
                record_access = getattr(self.store, "record_access", None)
                if record_access is not None:
                    try:
                        await record_access(match.memory_id)
                    except Exception:
                        _LOGGER.exception(
                            "Access recording failed after persistence for memory_id=%s",
                            match.memory_id,
                        )
        edge_enrichment_failed = False
        if self.edge_proposer is not None:
            for memory, candidates in edge_candidates:
                try:
                    proposed = await self._provider_call(
                        self.edge_proposer.propose, memory, candidates
                    )
                    for edge in proposed:
                        await self._add_edge(edge)
                except Exception:
                    edge_enrichment_failed = True
                    _LOGGER.exception(
                        "LLM edge enrichment failed for event_hash=%s event_id=%s memory_id=%s",
                        event_hash,
                        event.event_id,
                        memory.id,
                    )
        self._edge_enrichment_failures[event_hash] = edge_enrichment_failed
        return IngestResult(
            event_hash,
            memory_ids,
            edge_enrichment_failed=edge_enrichment_failed,
        )

    async def _extract(self, event: Event, event_hash: str) -> list[Any]:
        if self.extractor is None:
            return [self._raw_memory(event)]
        extract = getattr(self.extractor, "extract_atomic_memories", None)
        if extract is None:
            raise TypeError("extractor must provide extract_atomic_memories")
        try:
            extracted = list(await self._provider_call(
                extract, event.content, event_hash=event_hash
            ))
        except Exception:
            _LOGGER.exception(
                "Memory extraction failed; preserving the raw event as a memory"
            )
            extracted = []
        return extracted or [self._raw_memory(event)]

    async def _provider_call(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        """Run synchronous providers off-loop with a timeout and bounded retries."""

        for attempt in range(self.provider_retries + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(call, *args, **kwargs),
                    timeout=self.provider_timeout,
                )
            except Exception:
                if attempt == self.provider_retries:
                    raise
                _LOGGER.warning(
                    "Provider call failed; retrying attempt %d/%d",
                    attempt + 1,
                    self.provider_retries,
                    exc_info=True,
                )

    def _raw_memory(self, event: Event) -> Any:
        return SimpleNamespace(
            text=event.content,
            type=classify_memory(event.content, tool_name=event.tool_name).value,
            entities=list(event.metadata.get("entities", [])),
        )

    async def _retrieval_candidates(self, memory: Memory) -> list[Memory]:
        """Return the bounded, same-type ANN candidate set for one memory."""

        scored = await self.store.search(
            memory.embedding,
            memory.text,
            ANN_CANDIDATE_LIMIT,
            types=[memory.memory_type],
        )
        candidates: list[Memory] = []
        for item in scored:
            existing = await self.store.get(item.memory_id)
            if existing is not None and existing.id != memory.id:
                candidates.append(existing)
        return candidates

    async def _find_duplicate_matches(self, memory: Memory) -> list[DuplicateMatch]:
        candidates = await self._retrieval_candidates(memory)
        return find_duplicates(
            memory,
            candidates,
            threshold=self.duplicate_threshold,
            same_type=True,
        )

    @staticmethod
    def _memory_type(value: str, event: Event, text: str) -> MemoryType:
        if event.tool_name:
            return MemoryType.TOOL_RESULT
        try:
            return MemoryType(value)
        except ValueError:
            return MemoryType(classify_memory(text).value)

    async def _add_edge(self, edge: Edge) -> None:
        add_edge = getattr(self.store, "add_edge", None)
        if add_edge is not None:
            await add_edge(edge)

    async def _add_provenance(self, provenance: Provenance) -> None:
        add_provenance = getattr(self.store, "add_provenance", None)
        if add_provenance is not None:
            await add_provenance(provenance)

def utc_now() -> datetime:
    """Return an aware UTC timestamp for callers creating events."""

    return datetime.now(timezone.utc)
