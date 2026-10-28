"""PostgreSQL/pgvector implementation of the memory store contract."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Iterable, Sequence

from ctxflow.memory.store import (
    Edge,
    Memory,
    MemoryStore,
    MemoryTier,
    MemoryType,
    MemoryVersion,
    Relation,
    ScoredId,
    Subgraph,
    SourceType,
    ValidityState,
    Provenance,
)
from ctxflow.memory.run_events import RunEvent, event_hash, verify_event_chain


class PostgresMemoryStore:
    """Asyncpg-backed store; the schema is provided in migrations/."""

    def __init__(self, pool: object, *, embedding_dimension: int | None = None) -> None:
        self.pool = pool
        self.embedding_dimension = embedding_dimension

    def _checked_vector(self, values: Sequence[float] | None) -> str | None:
        if values is not None and self.embedding_dimension is not None:
            if len(values) != self.embedding_dimension:
                raise ValueError(
                    f"Expected {self.embedding_dimension} embedding dimensions; "
                    f"received {len(values)}"
                )
        return _vector(values)

    def _checked_stored_vector(self, value: object) -> object:
        if value is None or self.embedding_dimension is None:
            return value
        values = _embedding_values(value)
        if len(values) != self.embedding_dimension:
            raise ValueError(
                f"Stored embedding has {len(values)} dimensions; "
                f"expected {self.embedding_dimension}"
            )
        return value

    async def add(self, memory: Memory) -> str:
        vector = self._checked_vector(memory.embedding)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await self._insert_memory(connection, memory, vector)
                await self._insert_initial_version(connection, memory, vector)
        return memory.id

    async def _insert_memory(self, connection: object, memory: Memory, vector: str | None) -> None:
        await connection.execute(
                """INSERT INTO memories
                (id,text,memory_type,tier,created_at,session_id,project_id,source,importance,
                 access_count,current_version,validity,archived,entities,embedding,
                 embedding_model,token_counts)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (id) DO NOTHING""",
                memory.id, memory.text, memory.memory_type.value, memory.tier.value,
                memory.created_at, memory.session_id, memory.project_id, memory.source,
                memory.importance,
                memory.access_count, memory.current_version, memory.validity.value,
                memory.archived, memory.entities, vector, memory.embedding_model,
                json.dumps(memory.token_counts),
        )

    async def _insert_initial_version(
        self, connection: object, memory: Memory, vector: str | None
    ) -> None:
        await connection.execute(
                """INSERT INTO memory_versions
                (memory_id,version,text,embedding,created_at,reason,supersedes_version,actor)
                VALUES ($1,1,$2,$3,$4,'initial',NULL,'system')
                ON CONFLICT DO NOTHING""",
                memory.id, memory.text, vector, memory.created_at,
        )

    async def persist_ingestion(
        self,
        event_hash: str,
        event_id: str,
        memory: Memory,
        edge: Edge | None,
        provenance: Provenance,
    ) -> bool:
        """Persist one event and all derived rows atomically.

        Returning ``False`` means another transaction already owns the event.
        """

        vector = self._checked_vector(memory.embedding)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                claimed = await connection.fetchval(
                    """INSERT INTO ingestion_events (event_hash,event_id)
                    VALUES ($1,$2) ON CONFLICT (event_hash) DO NOTHING
                    RETURNING event_hash""",
                    event_hash,
                    event_id,
                )
                if claimed is None:
                    return False
                await self._insert_memory(connection, memory, vector)
                await self._insert_initial_version(connection, memory, vector)
                if edge is not None:
                    await self._insert_edge(connection, edge)
                await self._insert_provenance(connection, provenance)
        return True

    async def persist_ingestions(
        self,
        event_hash: str,
        event_id: str,
        memories: list[Memory],
        edges: list[Edge],
        provenances: list[Provenance],
    ) -> bool:
        """Persist core records and deterministic edges in one transaction.

        Optional LLM-proposed edges are added by ``EventIngestor`` only after
        this transaction commits, so provider failures cannot roll back the
        event or its core memory records.
        """

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                claimed = await connection.fetchval(
                    """INSERT INTO ingestion_events (event_hash,event_id)
                    VALUES ($1,$2) ON CONFLICT (event_hash) DO NOTHING
                    RETURNING event_hash""",
                    event_hash,
                    event_id,
                )
                if claimed is None:
                    return False
                for memory in memories:
                    vector = self._checked_vector(memory.embedding)
                    await self._insert_memory(connection, memory, vector)
                    await self._insert_initial_version(connection, memory, vector)
                for edge in edges:
                    await self._insert_edge(connection, edge)
                for provenance in provenances:
                    await self._insert_provenance(connection, provenance)
        return True

    async def get(self, memory_id: str, version: int | None = None,
                  as_of: datetime | None = None, *,
                  project_id: str = "default") -> Memory | None:
        async with self.pool.acquire() as connection:
            if version is None and as_of is None:
                row = await connection.fetchrow(
                    "SELECT * FROM memories WHERE id=$1 AND project_id=$2",
                    memory_id, project_id,
                )
            else:
                condition = "v.version=$2" if version is not None else "v.created_at <= $2"
                value = version if version is not None else as_of
                row = await connection.fetchrow(
                    f"""SELECT m.*, v.text AS version_text, v.embedding AS version_embedding,
                    v.version AS selected_version FROM memories m
                    JOIN memory_versions v ON v.memory_id=m.id
                    WHERE m.id=$1 AND m.project_id=$3 AND {condition}
                    ORDER BY v.version DESC LIMIT 1""",
                    memory_id, value, project_id,
                )
        return _memory_from_row(row)

    async def create_version(self, memory_id: str, new_text: str, reason: str,
                             supersedes_version: int | None, actor: str, *,
                             embedding: Sequence[float] | None = None,
                             project_id: str = "default") -> int:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """SELECT current_version, embedding FROM memories
                    WHERE id=$1 AND project_id=$2 FOR UPDATE""",
                    memory_id, project_id,
                )
                if row is None:
                    raise KeyError(f"Unknown memory: {memory_id}")
                current = int(row["current_version"])
                if supersedes_version is not None and supersedes_version != current:
                    raise ValueError("supersedes_version must reference the current version")
                next_version = current + 1
                vector = self._checked_vector(embedding)
                if vector is None:
                    vector = self._checked_stored_vector(row["embedding"])
                await connection.execute(
                    """INSERT INTO memory_versions
                    (memory_id,version,text,embedding,created_at,reason,supersedes_version,actor)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    memory_id, next_version, new_text, vector,
                    datetime.now(timezone.utc),
                    reason, supersedes_version, actor,
                )
                await connection.execute(
                    """UPDATE memories SET text=$2, embedding=$3, current_version=$4
                    WHERE id=$1""",
                    memory_id, new_text, vector, next_version,
                )
        return next_version

    async def search(self, query_emb: Sequence[float] | None, query_text: str, k: int,
                     *, types: Iterable[MemoryType] | None = None,
                     include_archived: bool = False, include_history: bool = False,
                     as_of: datetime | None = None,
                     project_id: str = "default") -> list[ScoredId]:
        if k <= 0:
            return []
        vector = self._checked_vector(query_emb)
        type_values = [item.value for item in types] if types is not None else None
        async with self.pool.acquire() as connection:
            if vector is None:
                if as_of is None:
                    rows = await connection.fetch(
                        """SELECT id, 0.0 AS score FROM memories
                        WHERE ($1 OR NOT archived)
                          AND ($2::text[] IS NULL OR memory_type = ANY($2))
                          AND ($3 OR validity <> 'superseded')
                          AND project_id = $5
                          AND ($4 = '' OR text ILIKE '%' || $4 || '%')
                        ORDER BY created_at DESC LIMIT $6""",
                        include_archived, type_values, include_history, query_text,
                        project_id, k,
                    )
                else:
                    rows = await connection.fetch(
                        """SELECT m.id, 0.0 AS score FROM memories m
                        JOIN LATERAL (
                            SELECT text FROM memory_versions
                            WHERE memory_id=m.id AND created_at <= $5
                            ORDER BY version DESC LIMIT 1
                        ) v ON TRUE
                        WHERE ($1 OR NOT m.archived)
                          AND ($2::text[] IS NULL OR m.memory_type = ANY($2))
                          AND ($3 OR m.validity <> 'superseded')
                          AND m.project_id = $6
                          AND ($4 = '' OR v.text ILIKE '%' || $4 || '%')
                        ORDER BY m.created_at DESC LIMIT $7""",
                        include_archived, type_values, include_history, query_text, as_of,
                        project_id, k,
                    )
            else:
                if as_of is None:
                    rows = await connection.fetch(
                        """SELECT id, 1 - (embedding <=> $1::vector) AS score
                        FROM memories
                        WHERE ($2 OR NOT archived)
                          AND ($3::text[] IS NULL OR memory_type = ANY($3))
                          AND ($4 OR validity <> 'superseded')
                          AND project_id = $5
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> $1::vector LIMIT $6""",
                        vector, include_archived, type_values, include_history, project_id, k,
                    )
                else:
                    rows = await connection.fetch(
                        """SELECT m.id, 1 - (v.embedding <=> $1::vector) AS score
                        FROM memories m
                        JOIN LATERAL (
                            SELECT embedding FROM memory_versions
                            WHERE memory_id=m.id AND created_at <= $5
                            ORDER BY version DESC LIMIT 1
                        ) v ON TRUE
                        WHERE ($2 OR NOT m.archived)
                          AND ($3::text[] IS NULL OR m.memory_type = ANY($3))
                          AND ($4 OR m.validity <> 'superseded')
                          AND m.project_id = $6
                          AND v.embedding IS NOT NULL
                        ORDER BY v.embedding <=> $1::vector LIMIT $7""",
                        vector, include_archived, type_values, include_history, as_of,
                        project_id, k,
                    )
        return [ScoredId(row["id"], float(row["score"])) for row in rows]

    async def neighbors(self, memory_id: str, *, depth: int,
                        relations: Iterable[Relation] | None = None,
                        direction: str = "both", max_nodes: int = 200,
                        fanout_cap: int = 20,
                        project_id: str = "default") -> Subgraph:
        if depth < 0 or max_nodes < 1 or fanout_cap < 1:
            raise ValueError("depth must be >= 0 and graph limits must be >= 1")
        if direction not in {"both", "in", "out"}:
            raise ValueError("direction must be 'both', 'in', or 'out'")
        allowed = [item.value for item in relations] if relations is not None else None
        ids = [memory_id]
        seen = {memory_id}
        selected_edges: dict[tuple[str, str, str], Edge] = {}
        frontier = [memory_id]
        async with self.pool.acquire() as connection:
            for _level in range(depth):
                if not frontier or len(ids) >= max_nodes:
                    break
                next_frontier: list[str] = []
                for current in frontier:
                    rows = await connection.fetch(
                        """SELECT src, dst, relation, weight, confidence,
                        created_by, created_at FROM graph_edges
                        WHERE (($1='both' AND (src=$2 OR dst=$2))
                           OR ($1='out' AND src=$2) OR ($1='in' AND dst=$2))
                          AND EXISTS (
                              SELECT 1 FROM memories ms
                              WHERE ms.id=graph_edges.src AND ms.project_id=$5
                          )
                          AND EXISTS (
                              SELECT 1 FROM memories md
                              WHERE md.id=graph_edges.dst AND md.project_id=$5
                          )
                          AND ($3::text[] IS NULL OR relation=ANY($3))
                        ORDER BY weight DESC, src, dst LIMIT $4""",
                        direction, current, allowed, fanout_cap, project_id,
                    )
                    for row in rows:
                        edge = _edge_from_row(row)
                        neighbor = edge.dst if edge.src == current else edge.src
                        if neighbor not in seen:
                            if len(ids) >= max_nodes:
                                continue
                            seen.add(neighbor)
                            ids.append(neighbor)
                            next_frontier.append(neighbor)
                        selected_edges[(edge.src, edge.dst, edge.relation.value)] = edge
                frontier = next_frontier
            memories = [_memory_from_row(row) for row in await connection.fetch(
                "SELECT * FROM memories WHERE id=ANY($1::text[]) AND project_id=$2",
                ids, project_id
            )]
        return Subgraph(
            [item for item in memories if item is not None],
            list(selected_edges.values()),
        )

    async def get_version_history(
        self, memory_id: str, *, project_id: str = "default"
    ) -> list[MemoryVersion]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT v.* FROM memory_versions v
                JOIN memories m ON m.id=v.memory_id
                WHERE v.memory_id=$1 AND m.project_id=$2 ORDER BY v.version""",
                memory_id, project_id
            )
        return [_version_from_row(row) for row in rows]

    async def record_access(self, memory_id: str) -> None:
        async with self.pool.acquire() as connection:
            updated = await connection.execute(
                """UPDATE memories
                SET access_count = access_count + 1,
                    importance = LEAST(1.0, importance + 0.05)
                WHERE id=$1""",
                memory_id,
            )
            if updated == "UPDATE 0":
                raise KeyError(f"Unknown memory: {memory_id}")

    async def add_edge(self, edge: Edge) -> None:
        async with self.pool.acquire() as connection:
            await self._insert_edge(connection, edge)

    async def _insert_edge(self, connection: object, edge: Edge) -> None:
        await connection.execute(
            """INSERT INTO graph_edges
            (src,dst,relation,weight,confidence,created_by,created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (src,dst,relation) DO UPDATE SET
            weight=EXCLUDED.weight, confidence=EXCLUDED.confidence""",
            edge.src, edge.dst, edge.relation.value, edge.weight, edge.confidence,
            edge.created_by, edge.created_at,
        )

    async def add_provenance(self, provenance: Provenance) -> None:
        async with self.pool.acquire() as connection:
            await self._insert_provenance(connection, provenance)

    async def get_provenance(
        self, memory_id: str, *, project_id: str = "default"
    ) -> Provenance | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT p.* FROM provenance p
                JOIN memories m ON m.id=p.memory_id
                WHERE p.memory_id=$1 AND m.project_id=$2""",
                memory_id, project_id,
            )
        if row is None:
            return None
        data = dict(row)
        return Provenance(
            memory_id=data["memory_id"],
            source_type=SourceType(data["source_type"]),
            source_id=data["source_id"],
            author=data["author"],
            tool=data["tool"],
            extraction_confidence=data["extraction_confidence"],
            parent_memories=list(data["parent_memories"] or []),
            created_at=data["created_at"],
        )

    async def _insert_provenance(self, connection: object, provenance: Provenance) -> None:
        await connection.execute(
            """INSERT INTO provenance
            (memory_id,source_type,source_id,author,tool,extraction_confidence,
             parent_memories,created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (memory_id) DO UPDATE SET
            source_type=EXCLUDED.source_type, source_id=EXCLUDED.source_id,
            author=EXCLUDED.author, tool=EXCLUDED.tool,
            extraction_confidence=EXCLUDED.extraction_confidence,
            parent_memories=EXCLUDED.parent_memories""",
            provenance.memory_id, provenance.source_type.value, provenance.source_id,
            provenance.author, provenance.tool, provenance.extraction_confidence,
            provenance.parent_memories, provenance.created_at,
        )

    async def claim_event(self, event_hash: str, event_id: str) -> bool:
        async with self.pool.acquire() as connection:
            status = await connection.execute(
                """INSERT INTO ingestion_events (event_hash,event_id)
                VALUES ($1,$2) ON CONFLICT (event_hash) DO NOTHING""",
                event_hash, event_id,
            )
        return status.endswith("1")

    async def append_run_event(
        self,
        run_id: str,
        project_id: str,
        event_type: str,
        event_data: dict[str, object],
    ) -> RunEvent:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    run_id,
                )
                previous = await connection.fetchrow(
                    """SELECT sequence, event_hash FROM run_events
                    WHERE run_id=$1 ORDER BY sequence DESC LIMIT 1 FOR UPDATE""",
                    run_id,
                )
                sequence = 0 if previous is None else int(previous["sequence"]) + 1
                previous_hash = None if previous is None else previous["event_hash"]
                digest = event_hash(
                    run_id=run_id,
                    project_id=project_id,
                    sequence=sequence,
                    event_type=event_type,
                    event_data=event_data,
                    previous_hash=previous_hash,
                )
                row = await connection.fetchrow(
                    """INSERT INTO run_events
                    (run_id, project_id, sequence, event_type, event_data,
                     previous_hash, event_hash)
                    VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)
                    RETURNING created_at""",
                    run_id, project_id, sequence, event_type,
                    json.dumps(event_data), previous_hash, digest,
                )
        return RunEvent(
            run_id, project_id, sequence, event_type, dict(event_data),
            previous_hash, digest, row["created_at"],
        )

    async def verify_run_event_chain(self, run_id: str) -> bool:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT run_id, project_id, sequence, event_type, event_data,
                previous_hash, event_hash, created_at
                FROM run_events WHERE run_id=$1 ORDER BY sequence""",
                run_id,
            )
        events = [
            RunEvent(
                row["run_id"], row["project_id"], row["sequence"],
                row["event_type"], dict(row["event_data"]),
                row["previous_hash"], row["event_hash"], row["created_at"],
            )
            for row in rows
        ]
        return verify_event_chain(events)


def _vector(values: Sequence[float] | None) -> str | None:
    return None if values is None else "[" + ",".join(str(float(v)) for v in values) + "]"


def _embedding_values(value: object) -> list[float]:
    if value is None:
        return []
    if isinstance(value, str):
        return [float(item) for item in value.strip("[]").split(",") if item]
    return [float(item) for item in value]  # type: ignore[union-attr]


def _json_object(value: object) -> dict[str, int]:
    if value is None:
        return {}
    if isinstance(value, str):
        return dict(json.loads(value))
    return dict(value)  # type: ignore[arg-type]


def _memory_from_row(row: object) -> Memory | None:
    if row is None:
        return None
    data = dict(row)
    return Memory(
        id=data["id"], text=data.get("version_text", data["text"]),
        memory_type=MemoryType(data["memory_type"]), tier=MemoryTier(data["tier"]),
        created_at=data["created_at"], session_id=data["session_id"],
        project_id=data.get("project_id", "default"), source=data["source"],
        importance=data["importance"], access_count=data["access_count"],
        current_version=data.get("selected_version", data["current_version"]),
        validity=ValidityState(data["validity"]), archived=data["archived"],
        entities=list(data["entities"] or []), embedding_model=data["embedding_model"],
        embedding=_embedding_values(data.get("version_embedding", data["embedding"])),
        token_counts=_json_object(data["token_counts"]),
    )


def _edge_from_row(row: object) -> Edge:
    data = dict(row)
    return Edge(data["src"], data["dst"], Relation(data["relation"]), data["weight"],
                data["confidence"], data["created_by"], data["created_at"])


def _version_from_row(row: object) -> MemoryVersion:
    data = dict(row)
    return MemoryVersion(data["memory_id"], data["version"], data["text"],
                         _embedding_values(data["embedding"]), data["created_at"], data["reason"],
                         data["supersedes_version"], data["actor"])
