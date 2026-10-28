"""Memory storage contracts and a deterministic in-memory store.

The protocol in this module is the Week 1-2 boundary for memory
work.  The in-memory implementation deliberately mirrors the append-only
versioning and bounded graph semantics that the PostgreSQL store will provide
later, so downstream work can be developed without a database.
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Protocol, Sequence
from uuid import uuid4


class MemoryType(str, Enum):
    """Controlled first-pass labels for stored memories."""

    CONVERSATION = "conversation"
    FACT = "fact"
    DECISION = "decision"
    OBSERVATION = "observation"
    TOOL_RESULT = "tool_result"
    DOCUMENT = "document"
    TASK = "task"
    CORRECTION = "correction"
    SUMMARY = "summary"
    USER_PREFERENCE = "user_preference"


class MemoryTier(str, Enum):
    """The three views over the single memories table."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class ValidityState(str, Enum):
    """Validity values reserved by the memory/trust contract."""

    VALID = "valid"
    STALE = "stale"
    CONTRADICTED = "contradicted"
    REQUIRES_REVIEW = "requires_review"
    SUPERSEDED = "superseded"


class Relation(str, Enum):
    """Relations shared by memory graph producers and consumers."""

    DERIVED_FROM = "derived_from"
    DEPENDS_ON = "depends_on"
    BASED_ON = "based_on"
    SUMMARY_OF = "summary_of"
    CAUSED_BY = "caused_by"
    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    DUPLICATE_OF = "duplicate_of"
    FOLLOWS = "follows"
    RELATED_TO = "related_to"


class SourceType(str, Enum):
    """Origins used by the provenance contract."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_OUTPUT = "tool_output"
    DOCUMENT = "document"
    DERIVED = "derived"
    EXTERNAL = "external"


def _now() -> datetime:
    """Return an aware UTC timestamp for deterministic storage semantics."""

    return datetime.now(timezone.utc)


def _new_id() -> str:
    """Create a storage identifier without coupling to the legacy Node ID."""

    return uuid4().hex[:16]


@dataclass
class Memory:
    """Current, denormalized view of one logical memory."""

    text: str
    id: str = field(default_factory=_new_id)
    memory_type: MemoryType = MemoryType.CONVERSATION
    tier: MemoryTier = MemoryTier.EPISODIC
    created_at: datetime = field(default_factory=_now)
    session_id: str | None = None
    project_id: str = "default"
    source: str = ""
    importance: float = 0.0
    access_count: int = 0
    current_version: int = 1
    validity: ValidityState = ValidityState.VALID
    archived: bool = False
    entities: list[str] = field(default_factory=list)
    embedding_model: str = ""
    embedding: list[float] = field(default_factory=list)
    token_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryVersion:
    """Immutable historical version of a memory."""

    memory_id: str
    version: int
    text: str
    embedding: list[float]
    created_at: datetime
    reason: str
    supersedes_version: int | None
    actor: str


@dataclass(frozen=True)
class Edge:
    """Typed, directed edge in the memory graph."""

    src: str
    dst: str
    relation: Relation
    weight: float = 1.0
    confidence: float = 1.0
    created_by: str = "rule"
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Provenance:
    """Source information captured when a memory is created."""

    memory_id: str
    source_type: SourceType
    source_id: str | None = None
    author: str | None = None
    tool: str | None = None
    extraction_confidence: float = 1.0
    parent_memories: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ScoredId:
    """Search result containing a memory identifier and fused score."""

    memory_id: str
    score: float


@dataclass(frozen=True)
class Subgraph:
    """Bounded graph result returned by neighbor traversal."""

    nodes: list[Memory]
    edges: list[Edge]


class MemoryStore(Protocol):
    """Async storage contract shared by production and test implementations."""

    async def add(self, memory: Memory) -> str: ...

    async def get(
        self,
        memory_id: str,
        version: int | None = None,
        as_of: datetime | None = None,
        *,
        project_id: str = "default",
    ) -> Memory | None: ...

    async def create_version(
        self,
        memory_id: str,
        new_text: str,
        reason: str,
        supersedes_version: int | None,
        actor: str,
        *,
        embedding: Sequence[float] | None = None,
        project_id: str = "default",
    ) -> int: ...

    async def search(
        self,
        query_emb: Sequence[float] | None,
        query_text: str,
        k: int,
        *,
        project_id: str = "default",
        types: Iterable[MemoryType] | None = None,
        include_archived: bool = False,
        include_history: bool = False,
        as_of: datetime | None = None,
    ) -> list[ScoredId]: ...

    async def neighbors(
        self,
        memory_id: str,
        *,
        project_id: str = "default",
        depth: int,
        relations: Iterable[Relation] | None = None,
        direction: str = "both",
        max_nodes: int = 200,
        fanout_cap: int = 20,
    ) -> Subgraph: ...

    async def get_version_history(
        self, memory_id: str, *, project_id: str = "default"
    ) -> list[MemoryVersion]: ...
