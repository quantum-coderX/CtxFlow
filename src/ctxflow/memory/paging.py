"""LRU page cache over a paged memory store; only touched pages stay in RAM."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import AsyncIterator

from ctxflow.memory.store import Edge, Memory, PagedMemoryStore, StorageTier


@dataclass(frozen=True)
class GraphPage:
    """A page of memories plus the edges that leave them."""

    memories: list[Memory]
    edges: list[Edge]
    next_cursor: str | None


@dataclass
class PageCacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


class PagedMemoryGraph:
    """Project-scoped, cached, bounded reads of a ``PagedMemoryStore``.

    Call :meth:`invalidate` after writes; entries also expire after ``ttl_s``.
    """

    def __init__(
        self,
        store: PagedMemoryStore,
        project_id: str,
        *,
        page_size: int = 100,
        max_pages: int = 32,
        ttl_s: float | None = 30.0,
    ) -> None:
        if page_size < 1 or max_pages < 1:
            raise ValueError("page_size and max_pages must be positive")
        self.store = store
        self.project_id = project_id
        self.page_size = page_size
        self.max_pages = max_pages
        self.ttl_s = ttl_s
        self.stats = PageCacheStats()
        self._pages: OrderedDict[
            tuple[StorageTier | None, str | None, int], tuple[float, GraphPage]
        ] = OrderedDict()

    async def page(
        self,
        cursor: str | None = None,
        *,
        tier: StorageTier | None = None,
        limit: int | None = None,
    ) -> GraphPage:
        size = limit or self.page_size
        key = (tier, cursor, size)
        cached = self._pages.get(key)
        if cached is not None and (
            self.ttl_s is None or time.monotonic() - cached[0] <= self.ttl_s
        ):
            self._pages.move_to_end(key)
            self.stats.hits += 1
            return cached[1]

        self.stats.misses += 1
        raw = await self.store.list_page(
            project_id=self.project_id, cursor=cursor, limit=size, tier=tier
        )
        edges = await self.store.edges_for(
            [memory.id for memory in raw.memories], project_id=self.project_id
        )
        page = GraphPage(raw.memories, edges, raw.next_cursor)
        self._pages[key] = (time.monotonic(), page)
        self._pages.move_to_end(key)
        while len(self._pages) > self.max_pages:
            self._pages.popitem(last=False)
            self.stats.evictions += 1
        return page

    async def iterate(
        self, *, tier: StorageTier | None = None
    ) -> AsyncIterator[GraphPage]:
        """Walk every page of a tier, in id order."""
        cursor: str | None = None
        while True:
            page = await self.page(cursor, tier=tier)
            yield page
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def invalidate(self) -> None:
        self._pages.clear()

    async def warm(
        self, *, tier: StorageTier = StorageTier.HOT, max_pages: int = 4
    ) -> int:
        """Preload up to *max_pages* pages of *tier*; returns pages now cached."""
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        loaded = 0
        async for _page in self.iterate(tier=tier):
            loaded += 1
            if loaded >= max_pages:
                break
        return loaded

    @property
    def cached_pages(self) -> int:
        return len(self._pages)
