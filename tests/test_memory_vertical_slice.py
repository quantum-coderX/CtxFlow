"""Week 3-6 memory vertical-slice tests."""

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from ctxflow.memory import (
    Edge,
    Event,
    EventIngestor,
    HashEmbedder,
    InMemoryMemoryStore,
    Memory,
    Relation,
    SimpleTokenizer,
    count_tokens,
)
from researchos.memory.context import MemoryContextProvider


def test_ingestion_is_idempotent_and_creates_session_edge():
    async def run():
        store = InMemoryMemoryStore()
        ingestor = EventIngestor(store, HashEmbedder(dimension=16))
        event = Event("e1", "s1", datetime.now(timezone.utc), "user", "remember that blue is preferred")

        first = await ingestor.ingest(event)
        second = await ingestor.ingest(event)

        assert second.duplicate is True
        assert second.memory_ids == first.memory_ids
        assert len(store.memories) == 1
        next_result = await ingestor.ingest(
            Event("e2", "s1", datetime.now(timezone.utc), "assistant", "The preference was applied")
        )
        assert (next_result.memory_ids[0], first.memory_ids[0], Relation.FOLLOWS) in store.edges

    asyncio.run(run())


def test_neighbors_respect_node_cap_and_embedding_is_deterministic():
    async def run():
        store = InMemoryMemoryStore()
        embedder = HashEmbedder(dimension=16)
        ingestor = EventIngestor(store, embedder)
        ids = []
        for index in range(5):
            result = await ingestor.ingest(
                Event(str(index), "s", datetime.now(timezone.utc), "user", f"memory {index}")
            )
            ids.append(result.memory_ids[0])
        for node_id in ids[1:]:
            await store.add_edge(Edge(ids[0], node_id, Relation.RELATED_TO))

        graph = await store.neighbors(ids[0], depth=1, max_nodes=3, fanout_cap=20)
        assert len(graph.nodes) <= 3
        assert await embedder.embed("same") == await embedder.embed("same")

    asyncio.run(run())


def test_token_counts_are_lazy_and_cached():
    memory = type("MemoryLike", (), {"text": "hello, world", "token_counts": {}})()
    tokenizer = SimpleTokenizer()
    assert count_tokens(memory, tokenizer) == 3
    assert memory.token_counts == {"simple-v1": 3}


def test_project_scope_isolation_and_context_budget():
    async def run():
        store = InMemoryMemoryStore()
        await store.add(
            Memory(
                id="project-a",
                project_id="a",
                text="project alpha finding",
            )
        )
        await store.add(
            Memory(
                id="project-b",
                project_id="b",
                text="project beta finding",
            )
        )

        result = await store.search(None, "finding", 10, project_id="a")
        assert [item.memory_id for item in result] == ["project-a"]
        assert await store.get("project-b", project_id="a") is None

        provider = MemoryContextProvider(store)
        context = await provider.retrieve("a", "finding", token_budget=10)
        assert [item.memory_id for item in context] == ["project-a"]
        assert context[0].tokens == 3

    asyncio.run(run())


def test_memory_context_provider_rejects_missing_project_scope():
    async def run():
        provider = MemoryContextProvider(InMemoryMemoryStore())
        with pytest.raises(ValueError, match="project_id"):
            await provider.retrieve("", "query", token_budget=10)

    asyncio.run(run())


def test_ingestion_enriches_memories_and_links_duplicates():
    async def run():
        store = InMemoryMemoryStore()
        ingestor = EventIngestor(
            store,
            HashEmbedder(dimension=16),
            duplicate_threshold=0.9,
        )

        first = await ingestor.ingest(
            Event("e1", "s1", datetime.now(timezone.utc), "user", "Fact: blue deployment")
        )
        second = await ingestor.ingest(
            Event("e2", "s1", datetime.now(timezone.utc), "user", "Fact: blue deployment")
        )

        memory = store.memories[first.memory_ids[0]]
        assert memory.memory_type.value == "fact"
        assert memory.token_counts == {"simple-v1": 4}
        assert 0 <= memory.importance <= 1
        assert (
            second.memory_ids[0],
            first.memory_ids[0],
            Relation.DUPLICATE_OF,
        ) in store.edges
        assert memory.access_count == 1
        assert memory.importance > 0

    asyncio.run(run())


def test_ingestion_preserves_core_records_when_edge_proposer_fails():
    async def run():
        class FailingProposer:
            def propose(self, _memory, neighbors):
                if not neighbors:
                    return []
                raise RuntimeError("provider unavailable")

        store = InMemoryMemoryStore()
        ingestor = EventIngestor(
            store,
            HashEmbedder(dimension=16),
            edge_proposer=FailingProposer(),
        )
        event = Event("edge-failure", "s1", datetime.now(timezone.utc), "user", "A durable fact")

        result = await ingestor.ingest(event)

        assert result.edge_enrichment_failed is False
        assert result.memory_ids
        assert len(store.memories) == 1
        assert len(store.versions[result.memory_ids[0]]) == 1
        assert result.memory_ids[0] in store.provenance

        second = await ingestor.ingest(
            Event("edge-failure-2", "s1", datetime.now(timezone.utc), "user", "A second fact")
        )
        assert second.edge_enrichment_failed is True
        assert len(store.memories) == 2
        assert len(store.versions[second.memory_ids[0]]) == 1
        assert second.memory_ids[0] in store.provenance

        replay = await ingestor.ingest(
            Event("edge-failure-2", "s1", datetime.now(timezone.utc), "user", "A second fact")
        )
        assert replay.duplicate is True
        assert replay.edge_enrichment_failed is True
        assert len(store.memories) == 2

    asyncio.run(run())


def test_ingestion_accepts_empty_edge_proposal():
    async def run():
        class EmptyProposer:
            def propose(self, _memory, _neighbors):
                return []

        store = InMemoryMemoryStore()
        ingestor = EventIngestor(
            store,
            HashEmbedder(dimension=16),
            edge_proposer=EmptyProposer(),
        )
        await ingestor.ingest(Event("empty-1", "s1", datetime.now(timezone.utc), "user", "first"))
        result = await ingestor.ingest(
            Event("empty-2", "s1", datetime.now(timezone.utc), "user", "second")
        )

        assert result.edge_enrichment_failed is False
        assert len(store.memories) == 2
        assert len(store.versions[result.memory_ids[0]]) == 1
        assert result.memory_ids[0] in store.provenance

    asyncio.run(run())


def test_ingestion_persists_successful_edge_proposal_after_core_records():
    async def run():
        class SuccessfulProposer:
            def propose(self, memory, neighbors):
                return [Edge(memory.id, neighbors[0].id, Relation.RELATED_TO, created_by="llm")]

        store = InMemoryMemoryStore()
        ingestor = EventIngestor(
            store,
            HashEmbedder(dimension=16),
            edge_proposer=SuccessfulProposer(),
        )
        first = await ingestor.ingest(
            Event("success-1", "s1", datetime.now(timezone.utc), "user", "first")
        )
        second = await ingestor.ingest(
            Event("success-2", "s1", datetime.now(timezone.utc), "user", "second")
        )

        assert second.edge_enrichment_failed is False
        assert (
            second.memory_ids[0],
            first.memory_ids[0],
            Relation.RELATED_TO,
        ) in store.edges
        assert second.memory_ids[0] in store.provenance

    asyncio.run(run())


def test_ingestion_survives_access_recording_failure_and_does_not_replay():
    async def run():
        class FailingAccessStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.access_attempts = 0

            async def record_access(self, memory_id):
                self.access_attempts += 1
                raise RuntimeError("access service unavailable")

        store = FailingAccessStore()
        ingestor = EventIngestor(store, HashEmbedder(dimension=16))
        await ingestor.ingest(
            Event("access-1", "s1", datetime.now(timezone.utc), "user", "Fact: durable")
        )
        result = await ingestor.ingest(
            Event("access-2", "s1", datetime.now(timezone.utc), "user", "Fact: durable")
        )

        assert result.memory_ids
        assert len(store.memories) == 2
        replay = await ingestor.ingest(
            Event("access-2", "s1", datetime.now(timezone.utc), "user", "Fact: durable")
        )
        assert replay.duplicate is True
        assert store.access_attempts == 1

    asyncio.run(run())


def test_ingestion_runs_blocking_extraction_off_loop_and_falls_back_on_timeout():
    async def run():
        loop_thread = threading.get_ident()
        provider_threads = []

        class SlowExtractor:
            def extract_atomic_memories(self, text, *, event_hash):
                provider_threads.append(threading.get_ident())
                time.sleep(0.05)
                return []

        store = InMemoryMemoryStore()
        ingestor = EventIngestor(
            store,
            HashEmbedder(dimension=16),
            SlowExtractor(),
            provider_timeout=0.01,
            provider_retries=0,
        )
        result = await ingestor.ingest(
            Event("timeout-1", "s1", datetime.now(timezone.utc), "user", "Keep this")
        )

        assert result.memory_ids
        assert store.memories[result.memory_ids[0]].text == "Keep this"
        assert provider_threads
        assert provider_threads[0] != loop_thread

    asyncio.run(run())


def test_ingestion_retries_cleanly_after_core_persistence_failure():
    async def run():
        class FlakyTransactionalStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.events = set()
                self.fail_once = True

            async def persist_ingestions(self, event_hash, event_id, memories, edges, provenances):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("transaction rolled back")
                if event_hash in self.events:
                    return False
                self.events.add(event_hash)
                for memory in memories:
                    await self.add(memory)
                for edge in edges:
                    await self.add_edge(edge)
                for provenance in provenances:
                    await self.add_provenance(provenance)
                return True

        store = FlakyTransactionalStore()
        ingestor = EventIngestor(store, HashEmbedder(dimension=16))
        event = Event("transaction-retry", "s1", datetime.now(timezone.utc), "user", "retry me")

        with pytest.raises(RuntimeError, match="rolled back"):
            await ingestor.ingest(event)
        assert not store.memories
        assert not store.events

        result = await ingestor.ingest(event)
        assert result.duplicate is False
        assert len(store.memories) == 1
        assert len(store.versions[result.memory_ids[0]]) == 1
        assert len(store.provenance) == 1
        assert len(store.events) == 1

    asyncio.run(run())
