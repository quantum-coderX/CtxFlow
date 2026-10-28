"""In-memory implementation of Week 1-2 memory contract."""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import replace
from datetime import datetime
from typing import Iterable, Sequence

from ctxflow.memory.store import (
    Edge,
    Memory,
    MemoryType,
    MemoryVersion,
    Provenance,
    Relation,
    ScoredId,
    Subgraph,
    _now,
)
from ctxflow.memory.run_events import RunEvent, event_hash, verify_event_chain


class InMemoryMemoryStore:
    """Small deterministic store used by fixtures and downstream unit tests."""

    def __init__(self) -> None:
        self.memories: dict[str, Memory] = {}
        self.versions: dict[str, list[MemoryVersion]] = {}
        self.edges: dict[tuple[str, str, Relation], Edge] = {}
        self.provenance: dict[str, Provenance] = {}
        self.run_events: dict[str, list[RunEvent]] = {}

    async def add(self, memory: Memory) -> str:
        """Insert a memory and its version-one snapshot idempotently."""

        if memory.id in self.memories:
            return memory.id
        self.memories[memory.id] = memory
        self.versions[memory.id] = [
            MemoryVersion(
                memory_id=memory.id,
                version=1,
                text=memory.text,
                embedding=list(memory.embedding),
                created_at=memory.created_at,
                reason="initial",
                supersedes_version=None,
                actor="system",
            )
        ]
        return memory.id

    async def get(
        self,
        memory_id: str,
        version: int | None = None,
        as_of: datetime | None = None,
        *,
        project_id: str = "default",
    ) -> Memory | None:
        """Return a current or historical snapshot without mutating storage."""

        memory = self.memories.get(memory_id)
        if memory is None or memory.project_id != project_id:
            return None
        if version is None and as_of is None:
            return replace(memory, embedding=list(memory.embedding))

        history = self.versions[memory_id]
        candidates = history
        if version is not None:
            candidates = [item for item in candidates if item.version == version]
        elif as_of is not None:
            candidates = [item for item in candidates if item.created_at <= as_of]
        if not candidates:
            return None
        snapshot = max(candidates, key=lambda item: item.version)
        return replace(
            memory,
            text=snapshot.text,
            embedding=list(snapshot.embedding),
            current_version=snapshot.version,
        )

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
    ) -> int:
        """Append a version and update only the memory's current pointer."""

        memory = self.memories.get(memory_id)
        if memory is None or memory.project_id != project_id:
            raise KeyError(f"Unknown memory: {memory_id}")
        next_version = len(self.versions[memory_id]) + 1
        if supersedes_version is not None and supersedes_version != memory.current_version:
            raise ValueError("supersedes_version must reference the current version")
        vector = list(memory.embedding if embedding is None else embedding)
        created_at = _now()
        self.versions[memory_id].append(
            MemoryVersion(
                memory_id=memory_id,
                version=next_version,
                text=new_text,
                embedding=vector,
                created_at=created_at,
                reason=reason,
                supersedes_version=supersedes_version,
                actor=actor,
            )
        )
        memory.text = new_text
        memory.embedding = vector
        memory.current_version = next_version
        return next_version

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
    ) -> list[ScoredId]:
        """Search with reciprocal-rank fusion of keyword and dense signals."""

        if k <= 0:
            return []
        allowed_types = set(types) if types is not None else None
        query_terms = set(re.findall(r"\w+", query_text.lower()))
        candidates: list[tuple[str, float, float]] = []
        for memory in self.memories.values():
            if memory.project_id != project_id:
                continue
            if memory.archived and not include_archived:
                continue
            if not include_history and memory.validity.value == "superseded":
                continue
            if allowed_types is not None and memory.memory_type not in allowed_types:
                continue
            snapshot = (
                await self.get(memory.id, as_of=as_of, project_id=project_id)
                if as_of else memory
            )
            if snapshot is None:
                continue
            keyword_score = self._keyword_score(query_terms, snapshot.text)
            dense_score = self._cosine(query_emb, snapshot.embedding)
            if keyword_score or dense_score or query_emb is not None or not query_terms:
                candidates.append((memory.id, keyword_score, dense_score))

        keyword_rank = self._rank({item[0]: item[1] for item in candidates})
        dense_rank = self._rank({item[0]: item[2] for item in candidates})
        fused = [
            ScoredId(memory_id=memory_id, score=(
                (1 / (60 + keyword_rank.get(memory_id, len(candidates) + 60)))
                + (1 / (60 + dense_rank.get(memory_id, len(candidates) + 60)))
            ))
            for memory_id, _, _ in candidates
        ]
        fused.sort(key=lambda item: (-item.score, item.memory_id))
        return fused[:k]

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
    ) -> Subgraph:
        """Return a bounded BFS subgraph, including the starting memory."""

        if (
            memory_id not in self.memories
            or self.memories[memory_id].project_id != project_id
        ):
            return Subgraph(nodes=[], edges=[])
        if depth < 0 or max_nodes < 1 or fanout_cap < 1:
            raise ValueError("depth must be >= 0 and graph limits must be >= 1")
        if direction not in {"both", "in", "out"}:
            raise ValueError("direction must be 'both', 'in', or 'out'")
        allowed = set(relations) if relations is not None else None
        distances = {memory_id: 0}
        queue: deque[str] = deque([memory_id])
        selected_edges: dict[tuple[str, str, Relation], Edge] = {}
        while queue and len(distances) < max_nodes:
            current = queue.popleft()
            if distances[current] >= depth:
                continue
            incident = [
                edge for edge in self.edges.values()
                if (direction in {"both", "out"} and edge.src == current)
                or (direction in {"both", "in"} and edge.dst == current)
            ]
            incident.sort(key=lambda edge: (-edge.weight, edge.src, edge.dst))
            for edge in incident[:fanout_cap]:
                if allowed is not None and edge.relation not in allowed:
                    continue
                selected_edges[(edge.src, edge.dst, edge.relation)] = edge
                neighbor = edge.dst if edge.src == current else edge.src
                if neighbor not in distances and len(distances) < max_nodes:
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)
        nodes = [
            self.memories[node_id]
            for node_id in distances
            if self.memories[node_id].project_id == project_id
        ]
        return Subgraph(nodes=nodes, edges=list(selected_edges.values()))

    async def get_version_history(
        self, memory_id: str, *, project_id: str = "default"
    ) -> list[MemoryVersion]:
        """Return all immutable versions in ascending version order."""

        if (
            memory_id not in self.memories
            or self.memories[memory_id].project_id != project_id
        ):
            return []
        return list(self.versions.get(memory_id, []))

    async def record_access(self, memory_id: str) -> None:
        """Record a deduplication hit on the canonical memory."""

        memory = self.memories.get(memory_id)
        if memory is None:
            raise KeyError(f"Unknown memory: {memory_id}")
        memory.access_count += 1
        memory.importance = min(1.0, memory.importance + 0.05)

    async def add_edge(self, edge: Edge) -> None:
        """Add or replace one typed edge after validating both endpoints."""

        if edge.src not in self.memories or edge.dst not in self.memories:
            raise KeyError("Both edge endpoints must exist in the memory store")
        if self.memories[edge.src].project_id != self.memories[edge.dst].project_id:
            raise ValueError("Memory edges cannot cross project boundaries")
        self.edges[(edge.src, edge.dst, edge.relation)] = edge

    async def add_provenance(self, provenance: Provenance) -> None:
        """Record one provenance row for an existing memory."""

        if provenance.memory_id not in self.memories:
            raise KeyError(f"Unknown memory: {provenance.memory_id}")
        self.provenance[provenance.memory_id] = provenance

    async def get_provenance(
        self, memory_id: str, *, project_id: str = "default"
    ) -> Provenance | None:
        """Return provenance only when the memory belongs to the requested project."""

        memory = self.memories.get(memory_id)
        if memory is None or memory.project_id != project_id:
            return None
        return self.provenance.get(memory_id)

    async def append_run_event(
        self,
        run_id: str,
        project_id: str,
        event_type: str,
        event_data: dict[str, object],
    ) -> RunEvent:
        events = self.run_events.setdefault(run_id, [])
        previous_hash = events[-1].event_hash if events else None
        item = RunEvent(
            run_id=run_id,
            project_id=project_id,
            sequence=len(events),
            event_type=event_type,
            event_data=dict(event_data),
            previous_hash=previous_hash,
            event_hash=event_hash(
                run_id=run_id,
                project_id=project_id,
                sequence=len(events),
                event_type=event_type,
                event_data=dict(event_data),
                previous_hash=previous_hash,
            ),
            created_at=_now(),
        )
        events.append(item)
        return item

    async def verify_run_event_chain(self, run_id: str) -> bool:
        return verify_event_chain(self.run_events.get(run_id, []))

    @staticmethod
    def _keyword_score(query_terms: set[str], text: str) -> float:
        terms = set(re.findall(r"\w+", text.lower()))
        return len(query_terms & terms) / len(query_terms) if query_terms else 0.0

    @staticmethod
    def _cosine(left: Sequence[float] | None, right: Sequence[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
            sum(value * value for value in right)
        )
        return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0

    @staticmethod
    def _rank(scores: dict[str, float]) -> dict[str, int]:
        ordered = sorted(scores, key=lambda key: (-scores[key], key))
        return {memory_id: rank for rank, memory_id in enumerate(ordered, start=1)}
