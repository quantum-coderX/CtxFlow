"""Literature Search Skill — arXiv, Semantic Scholar & OpenAlex client with caching.

Wraps external academic search APIs with:
- Normalized query strings (lowercase, whitespace collapse)
- Persistent SQLite TTL-based response cache
- Configurable max results and sort order
- Unified result format across providers
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# Default cache TTL: 7 days in seconds.
_DEFAULT_TTL = 7 * 24 * 3600

# arXiv API constants
_ARXIV_BASE = "http://export.arxiv.org/api/query"
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Semantic Scholar API constants
_S2_BASE = "https://api.semanticscholar.org/graph/v1/paper/search"

# OpenAlex API constants (keyless, polite pool via User-Agent)
_OPENALEX_BASE = "https://api.openalex.org/works"
_OPENALEX_UA = "ResearchOS/0.2 (https://github.com/ProjectZero24/CtxFlow)"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class SearchStatus(str, Enum):
    """Honest outcome of a provider search.

    ``partial`` means at least one provider succeeded and at least one failed;
    ``error`` means every requested provider failed.
    """

    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"


class ProviderParseError(ValueError):
    """A provider returned a response that could not be parsed."""


@dataclass
class PaperResult:
    """Unified paper result from any search provider.

    Attributes
    ----------
    title : str
        Paper title.
    authors : list[str]
        List of author names.
    abstract : str
        Paper abstract.
    year : int | None
        Publication year.
    doi : str
        DOI identifier (empty string if unavailable).
    arxiv_id : str
        arXiv identifier (empty string if unavailable).
    url : str
        Direct link to the paper.
    source : str
        Which API returned this result (``"arxiv"`` or ``"semantic_scholar"``).
    metadata : dict
        Provider-specific extra fields.
    """

    title: str = ""
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    year: Optional[int] = None
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderOutcome:
    """Result of one provider call, including failure information.

    The legacy ``search_arxiv`` / ``search_semantic_scholar`` functions still
    return a plain list. Use the ``*_outcome`` variants (or
    :meth:`LiteratureSearchSkill.search_with_outcome`) when the caller needs to
    distinguish "no matches" from "provider failed".
    """

    provider: str
    status: SearchStatus
    results: List[PaperResult] = field(default_factory=list)
    error: str = ""
    error_type: str = ""
    status_code: Optional[int] = None
    from_cache: bool = False
    elapsed_ms: float = 0.0
    transport: str = "http"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "result_count": len(self.results),
            "error": self.error,
            "error_type": self.error_type,
            "status_code": self.status_code,
            "from_cache": self.from_cache,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "transport": self.transport,
        }


@dataclass
class SearchOutcome:
    """Aggregate outcome of a (possibly multi-provider) search."""

    query: str
    provider: str
    outcomes: List[ProviderOutcome] = field(default_factory=list)
    results: List[PaperResult] = field(default_factory=list)
    status: SearchStatus = SearchStatus.EMPTY
    elapsed_ms: float = 0.0

    @property
    def errors(self) -> Dict[str, str]:
        """Map provider name -> error message for failed providers."""
        return {
            outcome.provider: outcome.error
            for outcome in self.outcomes
            if outcome.status is SearchStatus.ERROR and outcome.error
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "status": self.status.value,
            "result_count": len(self.results),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "providers": {
                outcome.provider: outcome.to_dict() for outcome in self.outcomes
            },
            "errors": self.errors,
        }


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


def _clean_error(exc: BaseException, limit: int = 500) -> str:
    message = " ".join(str(exc).split())
    return message[:limit]


def _status_code(exc: BaseException) -> Optional[int]:
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _paper_to_dict(paper: PaperResult) -> Dict[str, Any]:
    return {
        "title": paper.title,
        "authors": paper.authors,
        "abstract": paper.abstract,
        "year": paper.year,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "source": paper.source,
        "metadata": paper.metadata,
    }


# ---------------------------------------------------------------------------
# Cache Layer (SQLite)
# ---------------------------------------------------------------------------

class _QueryCache:
    """Persistent key-value cache backed by SQLite.

    One connection is opened per calling thread. SQLite connections are not
    shareable across threads, and the API runs the agent pipeline in a worker
    thread (``run_in_executor``) while tests and scripts may use the cache from
    the main thread. Per-thread connections keep both paths working; a lock
    serializes writes so concurrent workers cannot deadlock on the file lock.
    """

    def __init__(self, db_path: str, ttl: int = _DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._db_path = db_path
        self._local = threading.local()
        self._lock = threading.RLock()
        self._connections: List[sqlite3.Connection] = []
        # Create the schema eagerly so an empty cache file is ready before any
        # worker thread touches it.
        self._connection()

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._db_path, timeout=10.0, check_same_thread=False
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    ts    REAL NOT NULL
                )
                """
            )
            conn.commit()
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def get(self, key: str) -> Optional[str]:
        """Return cached value if present and not expired, else None."""
        with self._lock:
            row = self._connection().execute(
                "SELECT value, ts FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, ts = row
            if time.time() - ts > self.ttl:
                self._connection().execute(
                    "DELETE FROM cache WHERE key = ?", (key,)
                )
                self._connection().commit()
                return None
            return value

    def set(self, key: str, value: str) -> None:
        """Insert or update a cache entry."""
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, ts) VALUES (?, ?, ?)",
                (key, value, time.time()),
            )
            conn.commit()

    def clear(self) -> None:
        """Drop all cached entries."""
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM cache")
            conn.commit()

    def close(self) -> None:
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - best effort teardown
                    logger.debug("Ignoring cache close error", exc_info=True)
            self._connections.clear()
            self._local = threading.local()



def _normalize_query(query: str) -> str:
    """Normalize a search query for cache key generation."""
    return " ".join(query.lower().split())


def _cache_key(provider: str, query: str, max_results: int) -> str:
    """Generate a deterministic cache key."""
    normalized = _normalize_query(query)
    raw = f"{provider}:{normalized}:{max_results}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# arXiv Client
# ---------------------------------------------------------------------------

def search_arxiv(
    query: str,
    max_results: int = 5,
    sort_by: str = "relevance",
) -> List[PaperResult]:
    """Search arXiv via their Atom API (backwards-compatible wrapper).

    Returns only the results. Use :func:`search_arxiv_outcome` to distinguish
    an empty result set from a provider failure.
    """
    return search_arxiv_outcome(query, max_results=max_results, sort_by=sort_by).results


def search_arxiv_outcome(
    query: str,
    max_results: int = 5,
    sort_by: str = "relevance",
    timeout: float = 30.0,
) -> ProviderOutcome:
    """Search arXiv and return an explicit provider outcome.

    Parameters
    ----------
    query : str
        Search query string.
    max_results : int
        Maximum number of results.
    sort_by : str
        Sort order — ``"relevance"`` or ``"lastUpdatedDate"``.
    timeout : float
        Socket timeout in seconds.

    Returns
    -------
    ProviderOutcome
        ``ok`` with results, ``empty`` with no results, or ``error`` with the
        exception type/message. Malformed XML is an error, never an empty list.
    """
    import urllib.request

    started = time.monotonic()
    params = urllib.parse.urlencode({
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": "descending",
    })
    url = f"{_ARXIV_BASE}?{params}"

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            xml_data = resp.read().decode("utf-8")
        results = _parse_arxiv_xml(xml_data)
    except ProviderParseError as exc:
        logger.error("arXiv response parse failed: %s", exc)
        return ProviderOutcome(
            provider="arxiv",
            status=SearchStatus.ERROR,
            error=_clean_error(exc),
            error_type=type(exc).__name__,
            elapsed_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        logger.error("arXiv API request failed: %s", exc)
        return ProviderOutcome(
            provider="arxiv",
            status=SearchStatus.ERROR,
            error=_clean_error(exc),
            error_type=type(exc).__name__,
            status_code=_status_code(exc),
            elapsed_ms=_elapsed_ms(started),
        )

    return ProviderOutcome(
        provider="arxiv",
        status=SearchStatus.OK if results else SearchStatus.EMPTY,
        results=results,
        elapsed_ms=_elapsed_ms(started),
    )


def _parse_arxiv_xml(xml_data: str) -> List[PaperResult]:
    """Parse arXiv Atom XML into PaperResult list.

    Raises
    ------
    ProviderParseError
        If the payload is not valid XML. A valid XML document with no entries
        is a legitimate empty result, not an error.
    """
    results: List[PaperResult] = []
    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError as exc:
        raise ProviderParseError("arXiv response was not valid XML") from exc

    for entry in root.findall("atom:entry", _ARXIV_NS):
        title_el = entry.find("atom:title", _ARXIV_NS)
        summary_el = entry.find("atom:summary", _ARXIV_NS)
        id_el = entry.find("atom:id", _ARXIV_NS)
        published_el = entry.find("atom:published", _ARXIV_NS)

        authors = []
        for author_el in entry.findall("atom:author", _ARXIV_NS):
            name_el = author_el.find("atom:name", _ARXIV_NS)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        abstract = summary_el.text.strip() if summary_el is not None and summary_el.text else ""
        paper_url = id_el.text.strip() if id_el is not None and id_el.text else ""

        year = None
        if published_el is not None and published_el.text:
            try:
                year = int(published_el.text[:4])
            except (ValueError, IndexError):
                pass

        # Extract arXiv ID from the URL
        arxiv_id = ""
        if paper_url:
            arxiv_id = paper_url.rsplit("/", 1)[-1]

        results.append(PaperResult(
            title=title,
            authors=authors,
            abstract=abstract,
            year=year,
            arxiv_id=arxiv_id,
            url=paper_url,
            source="arxiv",
        ))

    return results


# ---------------------------------------------------------------------------
# Semantic Scholar Client
# ---------------------------------------------------------------------------

def search_semantic_scholar(
    query: str,
    max_results: int = 5,
    fields: Optional[List[str]] = None,
) -> List[PaperResult]:
    """Search Semantic Scholar via their Graph API (compatibility wrapper).

    Returns only the results. Use :func:`search_semantic_scholar_outcome` to
    distinguish an empty result set from a provider failure.
    """
    return search_semantic_scholar_outcome(
        query, max_results=max_results, fields=fields
    ).results


def search_semantic_scholar_outcome(
    query: str,
    max_results: int = 5,
    fields: Optional[List[str]] = None,
    timeout: float = 30.0,
) -> ProviderOutcome:
    """Search Semantic Scholar and return an explicit provider outcome."""
    import urllib.request

    started = time.monotonic()
    if fields is None:
        fields = ["title", "abstract", "authors", "year", "externalIds", "url"]

    params = urllib.parse.urlencode({
        "query": query,
        "limit": min(max_results, 100),
        "fields": ",".join(fields),
    })
    url = f"{_S2_BASE}?{params}"

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = _parse_s2_response(data)
    except ProviderParseError as exc:
        logger.error("Semantic Scholar response parse failed: %s", exc)
        return ProviderOutcome(
            provider="semantic_scholar",
            status=SearchStatus.ERROR,
            error=_clean_error(exc),
            error_type=type(exc).__name__,
            elapsed_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        logger.error("Semantic Scholar API request failed: %s", exc)
        return ProviderOutcome(
            provider="semantic_scholar",
            status=SearchStatus.ERROR,
            error=_clean_error(exc),
            error_type=type(exc).__name__,
            status_code=_status_code(exc),
            elapsed_ms=_elapsed_ms(started),
        )

    return ProviderOutcome(
        provider="semantic_scholar",
        status=SearchStatus.OK if results else SearchStatus.EMPTY,
        results=results,
        elapsed_ms=_elapsed_ms(started),
    )


def search_openalex(
    query: str,
    max_results: int = 5,
) -> List[PaperResult]:
    """Search OpenAlex (compatibility wrapper; returns results only)."""
    return search_openalex_outcome(query, max_results=max_results).results


def search_openalex_outcome(
    query: str,
    max_results: int = 5,
    timeout: float = 30.0,
) -> ProviderOutcome:
    """Search OpenAlex and return an explicit provider outcome.

    OpenAlex is keyless and returns abstracts as an inverted index, which is
    reconstructed into plain text. It is the third provider in ``both`` so a
    Semantic Scholar rate limit does not leave the demo with one source.
    """
    import urllib.request

    started = time.monotonic()
    params = urllib.parse.urlencode(
        {
            "search": query,
            "per-page": max(1, min(max_results, 50)),
        }
    )
    url = f"{_OPENALEX_BASE}?{params}"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": _OPENALEX_UA},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = _parse_openalex_response(data)
    except ProviderParseError as exc:
        logger.error("OpenAlex response parse failed: %s", exc)
        return ProviderOutcome(
            provider="openalex",
            status=SearchStatus.ERROR,
            error=_clean_error(exc),
            error_type=type(exc).__name__,
            elapsed_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        logger.error("OpenAlex API request failed: %s", exc)
        return ProviderOutcome(
            provider="openalex",
            status=SearchStatus.ERROR,
            error=_clean_error(exc),
            error_type=type(exc).__name__,
            status_code=_status_code(exc),
            elapsed_ms=_elapsed_ms(started),
        )

    return ProviderOutcome(
        provider="openalex",
        status=SearchStatus.OK if results else SearchStatus.EMPTY,
        results=results,
        elapsed_ms=_elapsed_ms(started),
    )


def _reconstruct_abstract(inverted_index: Any) -> str:
    """Rebuild abstract text from an OpenAlex ``abstract_inverted_index``."""
    if not isinstance(inverted_index, dict):
        return ""
    positioned: List[tuple] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                positioned.append((position, str(word)))
    positioned.sort()
    return " ".join(word for _, word in positioned)


def _parse_openalex_response(data: Dict[str, Any]) -> List[PaperResult]:
    """Parse an OpenAlex works response into ``PaperResult`` objects."""
    if not isinstance(data, dict):
        raise ProviderParseError("OpenAlex response was not a JSON object")

    works = data.get("results", [])
    if works is None:
        works = []
    if not isinstance(works, list):
        raise ProviderParseError("OpenAlex response 'results' field was not a list")
    if "error" in data and not works:
        raise ProviderParseError(str(data["error"]))

    results: List[PaperResult] = []
    for work in works:
        if not isinstance(work, dict):
            continue
        title = " ".join(
            str(work.get("title") or work.get("display_name") or "").split()
        )
        if not title:
            continue

        ids = work.get("ids") or {}
        arxiv_url = str(ids.get("arxiv") or "")
        arxiv_id = ""
        if arxiv_url:
            arxiv_id = arxiv_url.rsplit("/", 1)[-1].split("v")[0]

        authors = []
        for authorship in work.get("authorships") or []:
            author = (authorship or {}).get("author") or {}
            name = str(author.get("display_name") or "").strip()
            if name:
                authors.append(name)

        location = work.get("primary_location") or {}
        doi = str(work.get("doi") or "").replace("https://doi.org/", "")
        url = (
            str(location.get("landing_page_url") or "")
            or str(work.get("doi") or "")
            or str(work.get("id") or "")
        )

        results.append(
            PaperResult(
                title=title,
                authors=authors,
                abstract=_reconstruct_abstract(work.get("abstract_inverted_index")),
                year=work.get("publication_year"),
                doi=doi,
                arxiv_id=arxiv_id,
                url=url,
                source="openalex",
                metadata={"openalex_id": work.get("id") or ""},
            )
        )
    return results


def _parse_s2_response(data: Dict[str, Any]) -> List[PaperResult]:
    """Parse Semantic Scholar JSON response into PaperResult list.

    Raises
    ------
    ProviderParseError
        If the payload is not a search response object or ``data`` is not a
        list. A valid response with an empty ``data`` list is ``empty``.
    """
    if not isinstance(data, dict):
        raise ProviderParseError("Semantic Scholar response was not a JSON object")

    papers = data.get("data", [])
    if papers is None:
        papers = []
    if not isinstance(papers, list):
        raise ProviderParseError(
            "Semantic Scholar response 'data' field was not a list"
        )
    if "error" in data and not papers:
        raise ProviderParseError(str(data["error"]))

    results: List[PaperResult] = []
    for paper in papers:
        if not isinstance(paper, dict):
            raise ProviderParseError(
                "Semantic Scholar response contained a non-object paper entry"
            )
        authors = []
        for author in paper.get("authors", []):
            name = author.get("name", "")
            if name:
                authors.append(name)

        external_ids = paper.get("externalIds", {}) or {}
        doi = external_ids.get("DOI", "") or ""
        arxiv_id = external_ids.get("ArXiv", "") or ""

        results.append(PaperResult(
            title=paper.get("title", "") or "",
            authors=authors,
            abstract=paper.get("abstract", "") or "",
            year=paper.get("year"),
            doi=doi,
            arxiv_id=arxiv_id,
            url=paper.get("url", "") or "",
            source="semantic_scholar",
            metadata={"paperId": paper.get("paperId", "")},
        ))

    return results


# ---------------------------------------------------------------------------
# Unified Search with Caching
# ---------------------------------------------------------------------------

class LiteratureSearchSkill:
    """Unified literature search across arXiv and Semantic Scholar with caching.

    Parameters
    ----------
    cache_dir : str | None
        Directory for the SQLite cache database. Defaults to ``.cache/``
        in the current directory.
    cache_ttl : int
        Cache time-to-live in seconds (default: 7 days).
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        cache_ttl: int = _DEFAULT_TTL,
        arxiv_client: Optional[Any] = None,
    ) -> None:
        cache_path = Path(cache_dir or ".cache")
        cache_path.mkdir(parents=True, exist_ok=True)
        self._cache = _QueryCache(
            str(cache_path / "literature_cache.db"), ttl=cache_ttl
        )
        # Optional MCP-backed arXiv client (``researchos.mcp.ArxivMCPClient``).
        # When present, arXiv queries go through it first; any failure falls
        # back to the plain HTTP API on the same call.
        self._arxiv_client = arxiv_client
        self._mcp_calls = 0
        self._http_fallbacks = 0
        self._last_mcp_error: Optional[str] = None

    @property
    def transport_stats(self) -> Dict[str, Any]:
        """Counters describing how arXiv queries were served."""
        return {
            "mcp_calls": self._mcp_calls,
            "http_fallbacks": self._http_fallbacks,
            "last_mcp_error": self._last_mcp_error,
        }

    def search(
        self,
        query: str,
        max_results: int = 5,
        provider: str = "both",
    ) -> List[PaperResult]:
        """Search for papers (backwards-compatible list result).

        Use :meth:`search_with_outcome` when the caller needs to distinguish
        ``empty`` from ``error`` or inspect per-provider status.
        """
        return self.search_with_outcome(
            query=query, max_results=max_results, provider=provider
        ).results

    def search_with_outcome(
        self,
        query: str,
        max_results: int = 5,
        provider: str = "both",
    ) -> SearchOutcome:
        """Search for papers and return an explicit aggregate outcome.

        Parameters
        ----------
        query : str
            Search query.
        max_results : int
            Max results per provider.
        provider : str
            ``"arxiv"``, ``"semantic_scholar"``, ``"openalex"``, or ``"both"``
            (default; queries all three).

        Returns
        -------
        SearchOutcome
            ``status`` is ``ok`` (at least one result), ``empty`` (all
            providers returned no matches), ``partial`` (some providers failed),
            or ``error`` (all requested providers failed). Failed providers
            carry their exception type/message and are never cached.
        """
        started = time.monotonic()
        normalized = (provider or "both").strip().lower()

        if normalized == "both":
            providers = ["arxiv", "semantic_scholar", "openalex"]
        elif normalized in ("arxiv", "semantic_scholar", "openalex"):
            providers = [normalized]
        else:
            message = (
                f"unsupported provider {provider!r}; expected "
                "'arxiv', 'semantic_scholar', 'openalex', or 'both'"
            )
            logger.error(message)
            return SearchOutcome(
                query=query,
                provider=normalized,
                outcomes=[
                    ProviderOutcome(
                        provider=normalized,
                        status=SearchStatus.ERROR,
                        error=message,
                        error_type="ValueError",
                    )
                ],
                results=[],
                status=SearchStatus.ERROR,
                elapsed_ms=_elapsed_ms(started),
            )

        outcomes = [
            self._cached_search(name, query, max_results) for name in providers
        ]
        results = self._dedupe(
            [paper for outcome in outcomes for paper in outcome.results]
        )
        status = self._aggregate_status(outcomes)

        if status is SearchStatus.ERROR:
            logger.error("Literature search failed for %r: %s", query, {
                outcome.provider: outcome.error for outcome in outcomes
            })
        elif status is SearchStatus.PARTIAL:
            logger.warning("Literature search partially failed for %r: %s", query, {
                outcome.provider: outcome.error
                for outcome in outcomes
                if outcome.status is SearchStatus.ERROR
            })

        return SearchOutcome(
            query=query,
            provider=normalized,
            outcomes=outcomes,
            results=results,
            status=status,
            elapsed_ms=_elapsed_ms(started),
        )

    @staticmethod
    def _dedupe(results: List[PaperResult]) -> List[PaperResult]:
        """Deduplicate by title, keeping results that have no title.

        Metadata-based dedupe (DOI -> arXiv ID -> title) is a separate gap and
        is intentionally not changed here. Results without a title used to be
        dropped entirely; they are now kept and identified by DOI, arXiv ID, or
        URL when one is available.
        """
        seen_keys: set = set()
        deduped: List[PaperResult] = []
        for result in results:
            key = (
                (result.title or "").strip().lower()
                or (result.doi or "").strip().lower()
                or (result.arxiv_id or "").strip().lower()
                or (result.url or "").strip().lower()
            )
            if key:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            deduped.append(result)
        return deduped

    @staticmethod
    def _aggregate_status(outcomes: List[ProviderOutcome]) -> SearchStatus:
        if not outcomes:
            return SearchStatus.EMPTY
        statuses = {outcome.status for outcome in outcomes}
        if statuses == {SearchStatus.ERROR}:
            return SearchStatus.ERROR
        if SearchStatus.ERROR in statuses:
            return SearchStatus.PARTIAL
        if SearchStatus.OK in statuses:
            return SearchStatus.OK
        return SearchStatus.EMPTY

    def _cached_search(
        self, provider: str, query: str, max_results: int
    ) -> ProviderOutcome:
        """Return a provider outcome, using the cache only for success/empty."""
        key = _cache_key(provider, query, max_results)

        cached = self._cache.get(key)
        if cached is not None:
            outcome = self._deserialize_cache(provider, cached)
            if outcome is not None:
                outcome.from_cache = True
                outcome.elapsed_ms = 0.0
                logger.info("Cache hit for %s query: %s", provider, query)
                return outcome
            logger.warning("Ignoring unreadable cache entry for %s: %s", provider, query)

        logger.info("Cache miss for %s query: %s", provider, query)
        outcome = self._fetch_outcome(provider, query, max_results)

        # Never cache failures: a transient provider outage must not look like
        # "no matches" for the next seven days.
        if outcome.status in (SearchStatus.OK, SearchStatus.EMPTY):
            self._cache.set(key, self._serialize_cache(outcome))
        return outcome

    def _fetch_outcome(
        self, provider: str, query: str, max_results: int
    ) -> ProviderOutcome:
        if provider == "arxiv":
            mcp_outcome = self._fetch_arxiv_via_mcp(query, max_results)
            if mcp_outcome is not None:
                return mcp_outcome
            self._http_fallbacks += 1
            return search_arxiv_outcome(query, max_results=max_results)
        if provider == "semantic_scholar":
            return search_semantic_scholar_outcome(
                query, max_results=max_results
            )
        if provider == "openalex":
            return search_openalex_outcome(query, max_results=max_results)
        return ProviderOutcome(
            provider=provider,
            status=SearchStatus.ERROR,
            error=f"unsupported provider {provider!r}",
            error_type="ValueError",
        )

    def _fetch_arxiv_via_mcp(
        self, query: str, max_results: int
    ) -> Optional[ProviderOutcome]:
        """Search arXiv through the MCP client, or return ``None`` to fall back.

        Failure is intentionally soft: MCP is an enhancement, so a missing
        ``uvx``, a slow first download, or a protocol error must degrade to the
        HTTP provider instead of failing the research run.
        """
        client = self._arxiv_client
        if client is None:
            return None

        started = time.monotonic()
        try:
            papers = client.search(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001 — any transport error is a fallback
            self._last_mcp_error = _clean_error(exc)
            logger.warning(
                "arXiv MCP search failed (%s); falling back to the HTTP API",
                self._last_mcp_error,
            )
            return None

        self._mcp_calls += 1
        self._last_mcp_error = None
        return ProviderOutcome(
            provider="arxiv",
            status=SearchStatus.OK if papers else SearchStatus.EMPTY,
            results=list(papers),
            elapsed_ms=_elapsed_ms(started),
            transport="mcp",
        )

    @staticmethod
    def _serialize_cache(outcome: ProviderOutcome) -> str:
        return json.dumps({
            "status": outcome.status.value,
            "error": outcome.error,
            "error_type": outcome.error_type,
            "status_code": outcome.status_code,
            "transport": outcome.transport,
            "results": [_paper_to_dict(paper) for paper in outcome.results],
        })

    @staticmethod
    def _deserialize_cache(
        provider: str, cached: str
    ) -> Optional[ProviderOutcome]:
        """Read new or legacy cache entries; return None to force a refetch."""
        try:
            payload = json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            return None

        # Legacy format: a raw list of serialized PaperResult dicts.
        if isinstance(payload, list):
            try:
                results = [PaperResult(**item) for item in payload]
            except (TypeError, ValueError):
                return None
            return ProviderOutcome(
                provider=provider,
                status=SearchStatus.OK if results else SearchStatus.EMPTY,
                results=results,
            )

        if not isinstance(payload, dict):
            return None

        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            return None
        try:
            results = [PaperResult(**item) for item in raw_results]
        except (TypeError, ValueError):
            return None

        raw_status = payload.get("status")
        if raw_status is None:
            status = SearchStatus.OK if results else SearchStatus.EMPTY
        else:
            try:
                status = SearchStatus(raw_status)
            except ValueError:
                # Unknown status means the entry was written by a version we
                # do not understand: refetch instead of guessing "ok".
                logger.warning(
                    "Discarding cache entry with unknown status %r for %s",
                    raw_status,
                    provider,
                )
                return None
        if status is SearchStatus.ERROR:
            return None

        return ProviderOutcome(
            provider=provider,
            status=status,
            results=results,
            error=str(payload.get("error", "") or ""),
            error_type=str(payload.get("error_type", "") or ""),
            status_code=payload.get("status_code"),
            transport=str(payload.get("transport") or "http"),
        )

    def clear_cache(self) -> None:
        """Clear all cached search results."""
        self._cache.clear()

    def close(self) -> None:
        """Close the cache database connection."""
        self._cache.close()
