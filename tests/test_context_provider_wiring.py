import asyncio

from ctxflow.laya_provider import LayaMemoryContextProvider
from ctxflow.memory import InMemoryMemoryStore
from researchos.api.server import create_app
from researchos.memory.context import MemoryContextProvider


def test_memory_provider_is_optional_and_feature_flagged(monkeypatch):
    async def run():
        store = InMemoryMemoryStore()
        monkeypatch.delenv("LAYA_CONTEXT_SELECTION", raising=False)
        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "false")
        app = create_app(memory_store=store)
        async with app.router.lifespan_context(app):
            assert app.state.context_provider is None

        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
        app = create_app(memory_store=store)
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.context_provider, MemoryContextProvider)

        monkeypatch.setenv("LAYA_CONTEXT_SELECTION", "true")
        app = create_app(memory_store=store)
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.context_provider, LayaMemoryContextProvider)

    asyncio.run(run())


class _FakeRuntime:
    def __init__(self):
        self.store = InMemoryMemoryStore()
        self.closed = False

    async def close(self):
        self.closed = True


def _patch_runtime_create(monkeypatch, factory):
    from ctxflow.memory.runtime import PostgresMemoryRuntime

    monkeypatch.setattr(PostgresMemoryRuntime, "create", classmethod(lambda cls: factory()))


def test_lifespan_owns_and_closes_runtime_when_enabled(monkeypatch):
    runtime = _FakeRuntime()

    async def create():
        return runtime

    async def run():
        monkeypatch.delenv("LAYA_CONTEXT_SELECTION", raising=False)
        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
        _patch_runtime_create(monkeypatch, create)
        app = create_app()
        async with app.router.lifespan_context(app):
            assert app.state.memory_status == "runtime"
            assert isinstance(app.state.context_provider, MemoryContextProvider)
            assert app.state.context_provider.store is runtime.store
            assert not runtime.closed
        assert runtime.closed

    asyncio.run(run())


def test_runtime_failure_degrades_to_visible_disabled_memory(monkeypatch):
    async def create():
        raise ConnectionError("db down")

    async def run():
        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
        _patch_runtime_create(monkeypatch, create)
        app = create_app()
        async with app.router.lifespan_context(app):
            assert app.state.memory_status == "unavailable: ConnectionError"
            assert app.state.context_provider is None

    asyncio.run(run())


def test_runtime_not_created_when_memory_disabled(monkeypatch):
    async def create():
        raise AssertionError("runtime must not be created when memory is off")

    async def run():
        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "false")
        _patch_runtime_create(monkeypatch, create)
        app = create_app()
        async with app.router.lifespan_context(app):
            assert app.state.memory_status == "disabled"
            assert app.state.context_provider is None

    asyncio.run(run())
