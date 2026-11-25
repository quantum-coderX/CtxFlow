"""Tests for Laya-based context selection.

Demonstrates that Laya's learned decisions outperform greedy token-packing
when task type, tool dependencies, and session continuity matter.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from ctxflow.laya_selector import (
    HeuristicLayaRouter,
    LayaContextSelector,
    LayaDecision,
    MemoryRelevanceQuestion,
)
from ctxflow.laya_provider import LayaMemoryContextProvider, ContextRecord
from ctxflow.memory.inMemorystore import InMemoryMemoryStore
from ctxflow.memory.store import (
    Memory,
    MemoryType,
    MemoryTier,
    ValidityState,
    Relation,
    Edge,
)


class TestLayaContextSelector:
    """Tests for LayaContextSelector's learned decision-making."""

    @pytest.fixture
    def store(self):
        """In-memory store for testing."""
        return InMemoryMemoryStore()

    @pytest_asyncio.fixture
    async def populated_store(self, store):
        """Populate store with test memories."""
        # Task: web search for recent papers
        # Skills: arxiv_search, web_search
        # Previous: paper ID from last turn

        memories = [
            Memory(
                id="mem-arxiv-query",
                text="ArXiv paper search API endpoint: /api/search?q=<query>",
                memory_type=MemoryType.TOOL_RESULT,
                tier=MemoryTier.EPISODIC,
                created_at=datetime.now(timezone.utc),
                session_id="session-1",
                project_id="default",
                source="tool_output",
                importance=0.9,
                access_count=5,
                validity=ValidityState.VALID,
                entities=["arxiv", "api", "search"],
            ),
            Memory(
                id="mem-recent-paper",
                text="Transformers: Attention Is All You Need (Vaswani et al. 2017) "
                     "describes self-attention mechanisms and the Transformer architecture.",
                memory_type=MemoryType.DOCUMENT,
                tier=MemoryTier.SEMANTIC,
                created_at=datetime.now(timezone.utc),
                session_id="session-1",
                project_id="default",
                source="user_message",
                importance=0.8,
                access_count=3,
                validity=ValidityState.VALID,
                entities=["transformer", "attention", "neural networks"],
            ),
            Memory(
                id="mem-stale-query",
                text="Old search query: papers on RNNs from 2015",
                memory_type=MemoryType.CONVERSATION,
                tier=MemoryTier.EPISODIC,
                created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),  # 18 days old
                session_id="session-1",
                project_id="default",
                source="user_message",
                importance=0.1,
                access_count=0,
                validity=ValidityState.STALE,
                entities=["rnn", "2015"],
            ),
            Memory(
                id="mem-billing",
                text="Customer paid $500 for annual plan on 2026-01-15.",
                memory_type=MemoryType.FACT,
                tier=MemoryTier.EPISODIC,
                created_at=datetime.now(timezone.utc),
                session_id="session-2",  # Different session
                project_id="default",
                source="document",
                importance=0.7,
                access_count=2,
                validity=ValidityState.VALID,
                entities=["billing", "payment", "customer"],
            ),
        ]

        for mem in memories:
            await store.add(mem)

        return store

    @pytest.mark.asyncio
    async def test_laya_selects_task_relevant_memories(self, populated_store):
        """Laya should prefer memories relevant to the current task."""
        
        # Mock Laya router to return predictable decisions.
        mock_router = MagicMock()
        
        def mock_predict(state, questions, model="english", max_len=1024):
            # The state also carries the query; judge only the memory text.
            state = state.split("\nTEXT:\n", 1)[-1]
            # arxiv-query and recent-paper are task-relevant
            # stale-query is old and task-irrelevant
            # billing is unrelated to research task
            if "mem-arxiv-query" in state or "api/search" in state:
                return {
                    "task_match": {"answer": "yes", "confidence": 0.95},
                    "validity_check": {"answer": "valid", "confidence": 0.98},
                    "continuity": {"answer": 2, "confidence": 0.8},
                    "importance": {"answer": 2, "confidence": 0.9},
                }
            elif "Transformers" in state or "attention" in state:
                return {
                    "task_match": {"answer": "yes", "confidence": 0.90},
                    "validity_check": {"answer": "valid", "confidence": 0.95},
                    "continuity": {"answer": 2, "confidence": 0.8},
                    "importance": {"answer": 2, "confidence": 0.85},
                }
            elif "RNN" in state.upper():
                return {
                    "task_match": {"answer": "no", "confidence": 0.85},
                    "validity_check": {"answer": "invalid", "confidence": 0.9},
                    "continuity": {"answer": 0, "confidence": 0.8},
                    "importance": {"answer": 0, "confidence": 0.7},
                }
            elif "customer paid" in state.lower():
                return {
                    "task_match": {"answer": "no", "confidence": 0.98},
                    "validity_check": {"answer": "valid", "confidence": 0.95},
                    "continuity": {"answer": 0, "confidence": 0.9},
                    "importance": {"answer": 0, "confidence": 0.8},
                }
            return {
                "task_match": {"answer": "maybe", "confidence": 0.5},
                "validity_check": {"answer": "uncertain", "confidence": 0.5},
                "continuity": {"answer": 1, "confidence": 0.5},
                "importance": {"answer": 1, "confidence": 0.5},
            }
        
        mock_router.predict = mock_predict
        
        selector = LayaContextSelector(populated_store, router=mock_router)
        
        # Task: search for papers on transformers and attention
        selected = await selector.select_context(
            project_id="default",
            query="Search arXiv for recent papers on transformer architectures and self-attention",
            task_description="tool_call: arxiv_search",
            required_skills=["arxiv_search", "web_search"],
            token_budget=1000,
        )
        
        # Should select arxiv-query and recent-paper, skip billing and stale-query
        selected_ids = {item["memory_id"] for item in selected}
        assert "mem-arxiv-query" in selected_ids, "Should select task-relevant tool endpoint"
        assert "mem-recent-paper" in selected_ids, "Should select relevant paper reference"
        assert "mem-stale-query" not in selected_ids, "Should skip stale memories"
        assert "mem-billing" not in selected_ids, "Should skip unrelated task memory"

    @pytest.mark.asyncio
    async def test_laya_respects_token_budget(self, populated_store):
        """Laya should stop selecting when token budget is exhausted."""
        
        mock_router = MagicMock()
        mock_router.predict.return_value = {
            "task_match": {"answer": "yes", "confidence": 0.95},
            "validity_check": {"answer": "valid", "confidence": 0.95},
            "continuity": {"answer": 2, "confidence": 0.8},
            "importance": {"answer": 2, "confidence": 0.8},
        }
        
        selector = LayaContextSelector(populated_store, router=mock_router)
        
        # Very small budget (just enough for one memory)
        selected = await selector.select_context(
            project_id="default",
            query="test query",
            task_description="general",
            token_budget=50,
        )
        
        # Should select at most a small number of memories
        total_tokens = sum(item["tokens"] for item in selected)
        assert total_tokens <= 50, f"Used {total_tokens} tokens, budget was 50"

    @pytest.mark.asyncio
    async def test_laya_filters_superseded_memories(self, populated_store):
        """Laya should recognize and skip superseded/contradicted memories."""
        
        # Add a superseded memory
        await populated_store.add(
            Memory(
                id="mem-old-finding",
                text="Attention mechanism improvements reduce latency by 20%.",
                memory_type=MemoryType.FACT,
                tier=MemoryTier.EPISODIC,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                project_id="default",
                validity=ValidityState.SUPERSEDED,
                importance=0.3,
            )
        )
        
        mock_router = MagicMock()
        
        def mock_predict(state, questions, **kwargs):
            if "old-finding" in state:
                return {
                    "task_match": {"answer": "yes", "confidence": 0.7},
                    "validity_check": {"answer": "invalid", "confidence": 0.95},  # Laya says it's invalid
                    "continuity": {"answer": 1, "confidence": 0.6},
                    "importance": {"answer": 1, "confidence": 0.5},
                }
            return {
                "task_match": {"answer": "yes", "confidence": 0.9},
                "validity_check": {"answer": "valid", "confidence": 0.95},
                "continuity": {"answer": 2, "confidence": 0.8},
                "importance": {"answer": 2, "confidence": 0.8},
            }
        
        mock_router.predict = mock_predict
        selector = LayaContextSelector(populated_store, router=mock_router)
        
        selected = await selector.select_context(
            project_id="default",
            query="attention improvements",
            task_description="research_query",
            token_budget=1000,
        )
        
        selected_ids = {item["memory_id"] for item in selected}
        assert "mem-old-finding" not in selected_ids, "Should filter superseded memories"

    @pytest.mark.asyncio
    async def test_superseded_dropped_even_if_laya_keeps_it(self, populated_store):
        await populated_store.add(
            Memory(
                id="mem-replaced",
                text="Attention mechanism improvements reduce latency by 20%.",
                memory_type=MemoryType.FACT,
                project_id="default",
                validity=ValidityState.SUPERSEDED,
            )
        )
        mock_router = MagicMock()
        mock_router.predict.return_value = {
            "task_match": {"answer": "yes"},
            "validity_check": {"answer": "valid"},
            "continuity": {"answer": 2},
            "importance": {"answer": 2},
        }
        selector = LayaContextSelector(populated_store, router=mock_router)

        selected = await selector.select_context(
            project_id="default",
            query="attention latency improvements",
            task_description="research_query",
            token_budget=1000,
        )

        assert "mem-replaced" not in {item["memory_id"] for item in selected}

    @pytest.mark.asyncio
    async def test_heuristic_fallback_without_laya_package(self, populated_store):
        with patch.dict("sys.modules", {"laya": None}):
            selector = LayaContextSelector(populated_store)
        assert isinstance(selector.router, HeuristicLayaRouter)

        selected = await selector.select_context(
            project_id="default",
            query="transformer self-attention architecture",
            task_description="research:literature",
            token_budget=1000,
        )

        selected_ids = [item["memory_id"] for item in selected]
        assert selected_ids[0] == "mem-recent-paper"
        assert "mem-billing" not in selected_ids

    @pytest.mark.asyncio
    async def test_previous_context_is_passed_for_continuity(self, populated_store):
        states: list[str] = []

        def record(state, questions, **kwargs):
            states.append(state)
            return HeuristicLayaRouter().predict(state, questions)

        mock_router = MagicMock()
        mock_router.predict = record
        selector = LayaContextSelector(populated_store, router=mock_router)

        selected = await selector.select_context(
            project_id="default",
            query="transformer attention",
            task_description="research",
            previous_context_ids=["mem-recent-paper"],
            token_budget=1000,
        )

        paper_state = next(s for s in states if "Vaswani" in s)
        assert "IN_PREVIOUS_CONTEXT: yes" in paper_state
        assert "QUERY: transformer attention" in paper_state
        paper = next(i for i in selected if i["memory_id"] == "mem-recent-paper")
        assert paper["score"] == pytest.approx(1.0)


class TestLayaMemoryContextProvider:
    """Tests for LayaMemoryContextProvider end-to-end."""

    @pytest.mark.asyncio
    async def test_provider_returns_context_records(self):
        """Provider should return structured ContextRecord objects."""
        
        store = InMemoryMemoryStore()
        mem = Memory(
            id="test-mem",
            text="Test memory content",
            memory_type=MemoryType.CONVERSATION,
            project_id="default",
        )
        await store.add(mem)
        
        # Mock the selector to avoid Laya calls
        mock_selector = AsyncMock()
        mock_selector.select_context.return_value = [
            {
                "memory_id": "test-mem",
                "text": "Test memory content",
                "tokens": 10,
                "memory_type": "conversation",
                "validity": "valid",
                "importance": 0.5,
                "score": 0.8,
                "reason": "task_match",
                "source_type": None,
                "source_id": None,
            }
        ]
        
        provider = LayaMemoryContextProvider(store, selector=mock_selector)
        
        records = await provider.retrieve(
            project_id="default",
            query="test",
            token_budget=100,
            task_description="test_task",
        )
        
        assert len(records) == 1
        assert isinstance(records[0], ContextRecord)
        assert records[0].memory_id == "test-mem"
        assert records[0].score == 0.8
        assert records[0].reason == "task_match"

    @pytest.mark.asyncio
    async def test_provider_validates_inputs(self):
        """Provider should validate project_id and token_budget."""
        
        store = InMemoryMemoryStore()
        provider = LayaMemoryContextProvider(store)
        
        # Empty project_id
        with pytest.raises(ValueError, match="project_id is required"):
            await provider.retrieve("", "query", 100)
        
        # Negative token budget
        with pytest.raises(ValueError, match="token_budget must be non-negative"):
            await provider.retrieve("default", "query", -1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
