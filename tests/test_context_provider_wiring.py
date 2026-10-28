import asyncio

from ctxflow.memory import InMemoryMemoryStore
from researchos.api.server import create_app


def test_memory_provider_is_optional_and_feature_flagged(monkeypatch):
    async def run():
        store = InMemoryMemoryStore()
        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "false")
        app = create_app(memory_store=store)
        async with app.router.lifespan_context(app):
            assert app.state.context_provider is None

        monkeypatch.setenv("RESEARCHOS_MEMORY_ENABLED", "true")
        app = create_app(memory_store=store)
        async with app.router.lifespan_context(app):
            assert app.state.context_provider is not None

    asyncio.run(run())
