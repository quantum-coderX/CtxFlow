"""Synchronous project-graph backends for ``ResearchWorkspace``."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Optional, Protocol

from ctxflow.graph import ContextGraph
from ctxflow.graph_store import PostgresGraphStore

_BACKEND_ENV = "RESEARCHOS_GRAPH_BACKEND"
_CALL_TIMEOUT_S = 60.0


class GraphBackend(Protocol):
    """Where project graphs live when not using the JSON files."""

    name: str

    def load(self, project_id: str) -> Optional[ContextGraph]: ...

    def save(self, project_id: str, graph: ContextGraph) -> None: ...

    def close(self) -> None: ...


def graph_backend_choice(value: Optional[str] = None) -> str:
    """Return ``"json"`` (default) or ``"postgres"`` from ``RESEARCHOS_GRAPH_BACKEND``."""
    raw = (os.getenv(_BACKEND_ENV) if value is None else value) or "json"
    choice = raw.strip().lower()
    if choice not in {"json", "postgres"}:
        raise ValueError(f"{_BACKEND_ENV} must be 'json' or 'postgres'")
    return choice


class PostgresGraphBackend:
    """Runs the async store on a private event loop so sync callers never block it.

    Route handlers call ``ResearchWorkspace`` from the API loop thread; sharing
    that loop with a blocking ``result()`` would deadlock.
    """

    name = "postgres"

    def __init__(
        self,
        pool: Any,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
        *,
        page_size: int = 500,
    ) -> None:
        self._pool = pool
        self._loop = loop
        self._thread = thread
        self._store = PostgresGraphStore(pool, page_size=page_size)

    @classmethod
    def start(cls, *, dotenv_path: Any = None, page_size: int = 500) -> "PostgresGraphBackend":
        from ctxflow.memory.runtime import create_pool

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="graph-store-loop", daemon=True
        )
        thread.start()
        try:
            pool = asyncio.run_coroutine_threadsafe(
                create_pool(dotenv_path=dotenv_path), loop
            ).result(timeout=_CALL_TIMEOUT_S)
        except BaseException:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            raise
        return cls(pool, loop, thread, page_size=page_size)

    def _run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=_CALL_TIMEOUT_S
        )

    def load(self, project_id: str) -> Optional[ContextGraph]:
        return self._run(self._store.load_graph(project_id))

    def save(self, project_id: str, graph: ContextGraph) -> None:
        self._run(self._store.save_graph(project_id, graph))

    def close(self) -> None:
        try:
            self._run(self._pool.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
