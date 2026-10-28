"""General-web search skill with an explicit, replaceable provider.

The skill mirrors the academic search contract: a call returns a
:class:`WebSearchOutcome` whose ``status`` distinguishes ``ok``, ``empty`` and
``error`` so a provider failure is never reported as "no results".

Web search is opt-in. It requires ``RESEARCHOS_WEB_SEARCH=on`` and is also
disabled by ``RESEARCHOS_EXTERNAL_SEARCH=off`` so that the offline demo cannot
leave web requests enabled by accident.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlparse

from researchos.skills.literature_search import SearchStatus

logger = logging.getLogger(__name__)

WEB_PROVIDER_NAME = "web"
MAX_WEB_RESULTS = 10
_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSEY = frozenset({"0", "false", "no", "off", "disabled"})


def web_search_enabled() -> bool:
    """Whether general-web search is allowed by the environment."""
    external = str(os.environ.get("RESEARCHOS_EXTERNAL_SEARCH", "auto")).strip().lower()
    if external in _FALSEY:
        return False
    raw = str(os.environ.get("RESEARCHOS_WEB_SEARCH", "off")).strip().lower()
    return raw in _TRUTHY


@dataclass
class WebSearchResult:
    """One search hit. ``snippet`` is search-engine text, not source content."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = WEB_PROVIDER_NAME
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WebSearchOutcome:
    query: str
    provider: str = WEB_PROVIDER_NAME
    status: SearchStatus = SearchStatus.EMPTY
    results: List[WebSearchResult] = field(default_factory=list)
    error: str = ""
    error_type: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "status": self.status.value,
            "result_count": len(self.results),
            "error": self.error,
            "error_type": self.error_type,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class WebSearchProvider(Protocol):
    """Provider interface: return raw hits or raise on failure."""

    name: str

    def search(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """Return dicts with ``title``, ``url`` and ``snippet`` keys."""


class DDGSProvider:
    """DuckDuckGo adapter backed by the optional ``ddgs`` package."""

    name = "duckduckgo"

    def __init__(self, timeout: int = 8) -> None:
        self._timeout = timeout

    def search(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError(
                "web search provider 'ddgs' is not installed "
                "(pip install 'ctxflow[web]')"
            ) from exc
        raw = DDGS(timeout=self._timeout).text(query, max_results=max_results)
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("href") or item.get("url", ""),
                "snippet": item.get("body") or item.get("snippet", ""),
            }
            for item in (raw or [])
            if isinstance(item, dict)
        ]


class WebSearchSkill:
    """Run bounded general-web searches through a :class:`WebSearchProvider`."""

    def __init__(
        self,
        provider: Optional[WebSearchProvider] = None,
        *,
        enabled: Optional[bool] = None,
        max_results: int = 3,
    ) -> None:
        self._provider: WebSearchProvider = provider or DDGSProvider()
        self._enabled = enabled
        self.max_results = max(1, min(int(max_results), MAX_WEB_RESULTS))

    @property
    def enabled(self) -> bool:
        return web_search_enabled() if self._enabled is None else bool(self._enabled)

    def search_with_outcome(
        self, query: str, max_results: Optional[int] = None
    ) -> WebSearchOutcome:
        started = time.monotonic()
        limit = max(1, min(int(max_results or self.max_results), MAX_WEB_RESULTS))
        text = " ".join((query or "").split())
        if not text:
            return self._error(query, "query must be a non-empty string", "ValueError", started)
        if not self.enabled:
            return self._error(query, "web search is disabled", "Disabled", started)
        try:
            raw = self._provider.search(text, limit)
        except Exception as exc:  # noqa: BLE001 — provider failures are reported, not raised
            logger.warning("Web search failed for %r: %s", text[:80], exc)
            return self._error(query, str(exc), type(exc).__name__, started)
        if not isinstance(raw, list):
            return self._error(
                query, "provider returned a malformed response", "MalformedResponse", started
            )

        results: List[WebSearchResult] = []
        seen: set = set()
        for item in raw:
            hit = self._parse_hit(item)
            if hit is None or hit.url in seen:
                continue
            seen.add(hit.url)
            results.append(hit)
            if len(results) >= limit:
                break

        if raw and not results:
            return self._error(
                query, "provider returned no usable results", "MalformedResponse", started
            )
        return WebSearchOutcome(
            query=query,
            provider=WEB_PROVIDER_NAME,
            status=SearchStatus.OK if results else SearchStatus.EMPTY,
            results=results,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    def search(self, query: str, max_results: Optional[int] = None) -> List[WebSearchResult]:
        return self.search_with_outcome(query, max_results).results

    def _parse_hit(self, item: Any) -> Optional[WebSearchResult]:
        if not isinstance(item, dict):
            return None
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return WebSearchResult(
            title=" ".join(str(item.get("title") or "").split())[:300],
            url=url,
            snippet=" ".join(str(item.get("snippet") or "").split())[:1000],
            source=WEB_PROVIDER_NAME,
            metadata={"engine": getattr(self._provider, "name", "")},
        )

    @staticmethod
    def _error(query: str, message: str, error_type: str, started: float) -> WebSearchOutcome:
        return WebSearchOutcome(
            query=query,
            status=SearchStatus.ERROR,
            error=" ".join(message.split())[:500],
            error_type=error_type,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
