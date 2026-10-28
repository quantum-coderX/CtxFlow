"""arXiv search over MCP, using ``uvx arxiv-mcp-server``.

The MCP SDK is asynchronous while the rest of ResearchOS is synchronous, so
this module owns a small dedicated asyncio event loop running in a background
thread. One persistent ``ClientSession`` is kept alive for the process:

    client = ArxivMCPClient()
    client.search("graph retrieval augmented generation", max_results=5)

Design notes
------------
* **Lazy start.** Nothing is spawned until the first ``search`` call, so a
  demo that never searches externally pays no startup cost.
* **Fail closed, not loudly.** Any startup/call failure raises
  :class:`MCPUnavailable`. :class:`LiteratureSearchSkill` catches that and
  falls back to the plain arXiv HTTP API, which keeps the demo working even on
  a machine without ``uvx``.
* **Full abstracts.** ``search_papers`` is called with
  ``abstract_mode="full"`` so downstream claim extraction has real text.
* **Configurable via env** (all optional):

  - ``RESEARCHOS_MCP_ENABLED`` (default ``1``)
  - ``RESEARCHOS_MCP_COMMAND`` (default ``uvx``)
  - ``RESEARCHOS_MCP_ARGS`` (JSON array, or a shell-like string; default
    ``--from arxiv-mcp-server[pdf] arxiv-mcp-server``)
  - ``RESEARCHOS_MCP_TIMEOUT`` seconds per tool call (default ``60``)
  - ``RESEARCHOS_MCP_STARTUP_TIMEOUT`` seconds for first spawn, which may
    include a ``uvx`` package download (default ``180``)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shlex
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_COMMAND = "uvx"
_DEFAULT_ARGS = ["--from", "arxiv-mcp-server[pdf]", "arxiv-mcp-server"]
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_STARTUP_TIMEOUT = 180.0

_TOOL_NAME = "search_papers"


class MCPUnavailable(RuntimeError):
    """The MCP server could not be started or did not answer a call."""


def _env_flag(name: str, default: bool = True) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    return value if value > 0 else default


def _resolve_args() -> List[str]:
    raw = os.environ.get("RESEARCHOS_MCP_ARGS", "").strip()
    if not raw:
        return list(_DEFAULT_ARGS)
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("RESEARCHOS_MCP_ARGS is not valid JSON; using defaults")
            return list(_DEFAULT_ARGS)
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return list(parsed)
        logger.warning("RESEARCHOS_MCP_ARGS must be a JSON array of strings")
        return list(_DEFAULT_ARGS)
    return shlex.split(raw, posix=False)


def mcp_enabled() -> bool:
    """Whether MCP-backed arXiv search is switched on for this process."""
    return _env_flag("RESEARCHOS_MCP_ENABLED", True)


def _as_text_content(result: Any) -> str:
    """Concatenate the text blocks of an MCP ``CallToolResult``."""
    parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
        elif isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _extract_year(published: Any) -> Optional[int]:
    match = re.match(r"\s*(\d{4})", str(published or ""))
    return int(match.group(1)) if match else None


def parse_search_papers_result(result: Any) -> List[Dict[str, Any]]:
    """Turn an MCP ``search_papers`` result into plain paper dictionaries.

    Raises :class:`MCPUnavailable` when the tool reported an error or the
    payload is not the documented ``{"papers": [...]}`` object.
    """
    if getattr(result, "isError", False):
        detail = _as_text_content(result) or "unknown tool error"
        raise MCPUnavailable(f"search_papers failed: {detail}")

    text = _as_text_content(result)
    if not text:
        raise MCPUnavailable("search_papers returned no content")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPUnavailable(f"search_papers returned non-JSON content: {exc}") from exc

    if isinstance(payload, dict):
        papers = payload.get("papers")
    else:
        papers = payload
    if papers is None:
        papers = []
    if not isinstance(papers, list):
        raise MCPUnavailable("search_papers payload did not contain a paper list")

    normalized: List[Dict[str, Any]] = []
    for item in papers:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        arxiv_id = str(item.get("id") or "").strip()
        abstract = " ".join(str(item.get("abstract") or "").split())
        # The server marks truncated snippets with a trailing ellipsis; strip
        # the marker so callers do not mistake it for part of the sentence.
        abstract = re.sub(r"\s*\[truncated\]$", "", abstract).strip()
        normalized.append(
            {
                "title": title,
                "authors": [str(a) for a in (item.get("authors") or []) if a],
                "abstract": abstract,
                "year": _extract_year(item.get("published")),
                "arxiv_id": arxiv_id,
                "url": str(item.get("url") or ""),
                "categories": list(item.get("categories") or []),
                "versioned_id": str(item.get("versioned_id") or ""),
            }
        )
    return normalized


class ArxivMCPClient:
    """Synchronous facade over a persistent ``arxiv-mcp-server`` stdio session."""

    def __init__(
        self,
        *,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        call_timeout: Optional[float] = None,
        startup_timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.command = command or os.environ.get("RESEARCHOS_MCP_COMMAND", _DEFAULT_COMMAND)
        self.args = list(args) if args is not None else _resolve_args()
        self.call_timeout = (
            call_timeout
            if call_timeout is not None
            else _env_float("RESEARCHOS_MCP_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        self.startup_timeout = (
            startup_timeout
            if startup_timeout is not None
            else _env_float("RESEARCHOS_MCP_STARTUP_TIMEOUT", _DEFAULT_STARTUP_TIMEOUT)
        )
        self.enabled = mcp_enabled() if enabled is None else bool(enabled)

        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Any = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._available: Optional[bool] = None
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    @property
    def available(self) -> Optional[bool]:
        """``True``/``False`` once started; ``None`` before the first call."""
        return self._available

    @property
    def last_error(self) -> Optional[str]:
        return str(self._start_error) if self._start_error else None

    def _ensure_started(self) -> None:
        if not self.enabled:
            raise MCPUnavailable("MCP arXiv search is disabled (RESEARCHOS_MCP_ENABLED=0)")
        if self._closed:
            raise MCPUnavailable("MCP client is closed")

        with self._lock:
            if self._thread is not None:
                thread = self._thread
            else:
                self._ready.clear()
                self._start_error = None
                thread = threading.Thread(
                    target=self._thread_main,
                    name="researchos-arxiv-mcp",
                    daemon=True,
                )
                self._thread = thread
                thread.start()

        if not self._ready.wait(self.startup_timeout + 5):
            raise MCPUnavailable(
                f"arxiv-mcp-server did not start within {self.startup_timeout:.0f}s"
            )
        if self._start_error is not None or self._session is None:
            detail = self._start_error or "session was not created"
            raise MCPUnavailable(f"arxiv-mcp-server unavailable: {detail}")

    def close(self) -> None:
        """Stop the background session (idempotent)."""
        with self._lock:
            self._closed = True
            loop = self._loop
            shutdown = self._shutdown_event
            thread = self._thread
        if loop is not None and shutdown is not None:
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except RuntimeError:  # loop already closed
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        self._session = None
        self._available = False

    def __enter__(self) -> "ArxivMCPClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- background loop -------------------------------------------------
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except asyncio.CancelledError:  # normal shutdown
            pass
        except BaseException as exc:  # noqa: BLE001 — reported to callers
            self._start_error = exc
            logger.warning("arxiv-mcp-server session ended: %s", exc)
        finally:
            self._session = None
            if self._available is None:
                self._available = False
            self._ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # pragma: no cover - best effort
                pass
            loop.close()
            asyncio.set_event_loop(None)

    async def _serve(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise MCPUnavailable(
                "the 'mcp' package is not installed; install researchos[mcp]"
            ) from exc

        env = {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        # Inherit only the OS-level variables the MCP SDK considers safe
        # (PATH, TEMP, HOME, ...), never the parent's secrets.
        try:
            from mcp.client.stdio import get_default_environment

            env = {**get_default_environment(), **env}
        except Exception:  # pragma: no cover - older MCP versions
            pass
        params = StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=env,
        )
        shutdown = asyncio.Event()
        self._shutdown_event = shutdown

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(
                    session.initialize(), timeout=self.startup_timeout
                )
                self._session = session
                self._available = True
                logger.info(
                    "arxiv-mcp-server ready via %s %s",
                    self.command,
                    " ".join(self.args),
                )
                self._ready.set()
                await shutdown.wait()

    # -- calls -----------------------------------------------------------
    async def _call_search(
        self, query: str, max_results: int
    ) -> List[Dict[str, Any]]:
        session = self._session
        if session is None:
            raise MCPUnavailable("session is not available")
        result = await session.call_tool(
            _TOOL_NAME,
            {
                "query": query,
                "max_results": max(1, min(int(max_results), 50)),
                "abstract_mode": "full",
            },
        )
        return parse_search_papers_result(result)

    def search_as_dicts(
        self, query: str, max_results: int = 5
    ) -> List[Dict[str, Any]]:
        """Search arXiv and return provider-neutral paper dictionaries."""
        if max_results <= 0:
            return []
        self._ensure_started()
        loop = self._loop
        if loop is None:
            raise MCPUnavailable("background event loop is not running")

        future = asyncio.run_coroutine_threadsafe(
            self._call_search(query, max_results), loop
        )
        try:
            return future.result(timeout=self.call_timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise MCPUnavailable(
                f"search_papers timed out after {self.call_timeout:.0f}s"
            ) from exc
        except MCPUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPUnavailable(f"search_papers failed: {exc}") from exc

    def search(self, query: str, max_results: int = 5) -> List[Any]:
        """Search arXiv and return :class:`PaperResult` objects."""
        from researchos.skills.literature_search import PaperResult

        papers = []
        for item in self.search_as_dicts(query, max_results=max_results):
            metadata: Dict[str, Any] = {
                "mcp": True,
                "provider": "arxiv-mcp",
                "categories": item.get("categories") or [],
            }
            if item.get("versioned_id"):
                metadata["versioned_id"] = item["versioned_id"]
            papers.append(
                PaperResult(
                    title=item["title"],
                    authors=list(item.get("authors") or []),
                    abstract=str(item.get("abstract") or ""),
                    year=item.get("year"),
                    arxiv_id=str(item.get("arxiv_id") or ""),
                    url=str(item.get("url") or ""),
                    source="arxiv-mcp",
                    metadata=metadata,
                )
            )
        return papers
