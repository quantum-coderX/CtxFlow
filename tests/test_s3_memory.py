import asyncio

import pytest

from ctxflow.memory import RetentionPolicy, purge_run_events
from ctxflow.memory import InMemoryMemoryStore
from researchos.memory import create_context_provider, memory_enabled


def test_memory_flag_is_disabled_by_default_and_accepts_boolean_values(monkeypatch):
    monkeypatch.delenv("RESEARCHOS_MEMORY_ENABLED", raising=False)
    assert memory_enabled(None) is False
    assert memory_enabled("true") is True
    assert memory_enabled("OFF") is False


def test_memory_flag_rejects_ambiguous_values():
    with pytest.raises(ValueError, match="RESEARCHOS_MEMORY_ENABLED"):
        memory_enabled("maybe")


def test_context_provider_factory_is_optional():
    store = object()
    assert create_context_provider(store, enabled="false") is None
    provider = create_context_provider(store, enabled="true")
    assert provider is not None


def test_retention_policy_rejects_non_positive_windows():
    with pytest.raises(ValueError, match="run_events_days"):
        RetentionPolicy(run_events_days=0)


def test_purge_run_events_uses_the_configured_window():
    class FakePool:
        def __init__(self):
            self.calls = []

        async def execute(self, query, days):
            self.calls.append((query, days))
            return "DELETE 7"

    async def run():
        pool = FakePool()
        deleted = await purge_run_events(pool, RetentionPolicy(run_events_days=30))
        assert deleted == 7
        assert pool.calls[0][1] == 30
        assert "run_events" in pool.calls[0][0]

    asyncio.run(run())


def test_run_event_chain_detects_tampering():
    async def run():
        store = InMemoryMemoryStore()
        first = await store.append_run_event("run-1", "project-a", "started", {"step": 1})
        await store.append_run_event("run-1", "project-a", "finished", {"step": 2})
        assert first.sequence == 0
        assert await store.verify_run_event_chain("run-1") is True
        store.run_events["run-1"][1] = store.run_events["run-1"][1].__class__(
            **{**store.run_events["run-1"][1].__dict__, "event_data": {"step": 9}}
        )
        assert await store.verify_run_event_chain("run-1") is False

    asyncio.run(run())
