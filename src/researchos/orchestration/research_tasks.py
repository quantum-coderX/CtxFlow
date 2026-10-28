"""Query decomposition and bounded parallel research workers.

This module owns three things the ``retrieve`` stage needs:

* a validated :class:`ResearchPlan` (2-4 focused subtasks for a compound
  question, one for a simple one) with a deterministic fallback planner;
* normalized :class:`SourceRecord` objects with identifier-first deduplication;
* :class:`ResearchTaskExecutor`, which fans subtasks out over a capped thread
  pool, fans results back in with a stable ordering, and fetches a bounded
  number of web pages.

Models only suggest query text and purpose. Providers and tools are chosen by
code from a fixed allowlist.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from researchos.guardrails import allow_tool
from researchos.orchestration.state import make_task_event
from researchos.skills.literature_search import SearchStatus
from researchos.skills.source_fetch import FetchStatus, canonical_url

logger = logging.getLogger(__name__)

ACADEMIC_PROVIDERS: Tuple[str, ...] = ("arxiv", "semantic_scholar", "openalex")
WEB_PROVIDER = "web"
ALLOWED_PROVIDERS: Tuple[str, ...] = ACADEMIC_PROVIDERS + (WEB_PROVIDER,)

MAX_TASK_QUERY_CHARS = 300
MAX_TASK_PURPOSE_CHARS = 240
_MIN_TITLE_KEY_CHARS = 20

ACCESS_STATUSES = ("abstract_only", "full_text", "unavailable", "failed")

EventSink = Callable[[Dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def _env_number(name: str, default: float, cast: Callable[[Any], Any] = float) -> Any:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ResearchLimits:
    """Explicit bounds for one research run (demo defaults)."""

    max_subtasks: int = 4
    max_workers: int = 2
    results_per_provider: int = 3
    max_fetched_pages: int = 4
    max_merged_sources: int = 12
    fetch_timeout_s: float = 8.0
    max_page_bytes: int = 1_000_000
    max_research_s: float = 60.0

    @classmethod
    def from_env(cls) -> "ResearchLimits":
        defaults = cls()
        return cls(
            max_subtasks=min(4, _env_number("RESEARCHOS_MAX_SUBTASKS", defaults.max_subtasks, int)),
            max_workers=_env_number("RESEARCHOS_MAX_WORKERS", defaults.max_workers, int),
            results_per_provider=_env_number(
                "RESEARCHOS_RESULTS_PER_PROVIDER", defaults.results_per_provider, int
            ),
            max_fetched_pages=_env_number(
                "RESEARCHOS_MAX_FETCHED_PAGES", defaults.max_fetched_pages, int
            ),
            max_merged_sources=defaults.max_merged_sources,
            fetch_timeout_s=_env_number("RESEARCHOS_FETCH_TIMEOUT_S", defaults.fetch_timeout_s),
            max_page_bytes=_env_number(
                "RESEARCHOS_MAX_PAGE_BYTES", defaults.max_page_bytes, int
            ),
            max_research_s=_env_number("RESEARCHOS_MAX_RESEARCH_S", defaults.max_research_s),
        )

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class PlanValidationError(ValueError):
    """A proposed research plan broke the contract."""


@dataclass(frozen=True)
class ResearchTask:
    task_id: str
    query: str
    purpose: str
    providers: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "purpose": self.purpose,
            "providers": list(self.providers),
        }


@dataclass(frozen=True)
class ResearchPlan:
    primary_question: str
    subtasks: Tuple[ResearchTask, ...]
    planner: str  # "model" | "deterministic_fallback"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_question": self.primary_question,
            "subtasks": [task.to_dict() for task in self.subtasks],
            "planner": self.planner,
        }


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def validate_plan_tasks(
    raw_tasks: Any,
    *,
    providers: Sequence[str],
    max_tasks: int = 4,
    min_tasks: int = 1,
) -> Tuple[ResearchTask, ...]:
    """Validate proposed subtasks and return immutable, policy-bound tasks.

    Only ``query`` and ``purpose`` are taken from the proposal. A ``providers``
    entry, if present, must be drawn from the allowlist or the plan is rejected;
    the providers that actually run are always the application's.
    """
    bad_policy = [p for p in providers if p not in ALLOWED_PROVIDERS]
    if bad_policy:
        raise PlanValidationError(f"providers outside the allowlist: {bad_policy}")
    if not isinstance(raw_tasks, list):
        raise PlanValidationError("subtasks must be a list")
    if len(raw_tasks) > max_tasks:
        raise PlanValidationError(f"more than {max_tasks} subtasks")
    if len(raw_tasks) < min_tasks:
        raise PlanValidationError(f"fewer than {min_tasks} subtasks")

    tasks: List[ResearchTask] = []
    seen: set = set()
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            raise PlanValidationError("each subtask must be an object")
        query = " ".join(str(item.get("query") or "").split())
        purpose = " ".join(str(item.get("purpose") or "").split())
        if not query:
            raise PlanValidationError("subtask query must be non-empty")
        if len(query) > MAX_TASK_QUERY_CHARS:
            raise PlanValidationError(f"subtask query exceeds {MAX_TASK_QUERY_CHARS} characters")
        if len(purpose) > MAX_TASK_PURPOSE_CHARS:
            raise PlanValidationError(f"subtask purpose exceeds {MAX_TASK_PURPOSE_CHARS} characters")
        proposed = item.get("providers")
        if proposed is not None:
            if not isinstance(proposed, list) or any(
                p not in ALLOWED_PROVIDERS for p in proposed
            ):
                raise PlanValidationError("subtask names a provider outside the allowlist")
        key = _norm(query)
        if key in seen:
            raise PlanValidationError("duplicate subtask queries")
        seen.add(key)
        tasks.append(
            ResearchTask(
                task_id=f"task-{index:02d}",
                query=query,
                purpose=purpose,
                providers=tuple(providers),
            )
        )
    return tuple(tasks)


_STOPWORDS = frozenset(
    "the a an of and or to in on for with from by as at is are was were be been "
    "do does did how what why which where when who whether that this these those "
    "can could should would will than then also remain remains compared versus vs "
    "improve improves about into over under between their its it".split()
)
_QUESTION_START = r"(?:what|how|why|which|where|when|who|whether|does|do|is|are|can|compare)"
_SPLIT_PATTERN = re.compile(rf"\s*[;,]?\s+and\s+(?={_QUESTION_START}\b)", re.IGNORECASE)


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower()) if w not in _STOPWORDS]


_QUESTION_LEAD = re.compile(
    r"^(?:(?:how|what|why|which|where|when|who|whether)\s+)(?:(?:do|does|did|is|are|can|could|should|would)\s+)?",
    re.IGNORECASE,
)


def _strip_question_lead(part: str) -> str:
    stripped = _QUESTION_LEAD.sub("", part, count=1).strip()
    return stripped if len(stripped.split()) >= 3 else part


def split_question(question: str, max_parts: int = 4) -> List[str]:
    """Deterministically split a compound question into focused search queries."""
    text = " ".join((question or "").split())
    segments: List[str] = []
    for sentence in re.split(r"(?<=[?;])\s+", text):
        segments.extend(_SPLIT_PATTERN.split(sentence))
    parts = []
    for segment in segments:
        cleaned = segment.strip(" ?.;,")
        if len(cleaned) >= 12:
            parts.append(cleaned)
    if len(parts) < 2:
        return []

    lead_terms = _tokens(parts[0])
    focused: List[str] = []
    seen: set = set()
    for index, part in enumerate(parts[:max_parts]):
        query = _strip_question_lead(part)
        if index > 0:
            present = set(_tokens(part))
            context = [t for t in lead_terms if t not in present][:4]
            if context:
                query = f"{part} {' '.join(context)}"
        key = _norm(query)
        if key and key not in seen:
            seen.add(key)
            focused.append(query[:MAX_TASK_QUERY_CHARS])
    return focused if len(focused) >= 2 else []


_PLANNER_SYSTEM = (
    "You plan literature research. Split the user's question into 2 to 4 focused, "
    "non-overlapping search subtasks. Return JSON: "
    '{"subtasks": [{"query": "<search string, max 200 chars>", "purpose": "<why>"}]}. '
    "Do not name tools, providers, or URLs."
)


def llm_planning_enabled() -> bool:
    raw = str(os.environ.get("RESEARCHOS_LLM_PLANNING", "")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def plan_query(
    question: str,
    *,
    providers: Sequence[str],
    llm: Any = None,
    max_tasks: int = 4,
) -> ResearchPlan:
    """Build the validated plan for *question*.

    The model is consulted only for compound questions; a missing, failing, or
    non-conforming model falls back to the deterministic splitter.
    """
    providers = tuple(providers)
    parts = split_question(question, max_parts=max_tasks)
    complete_json = getattr(llm, "complete_json", None)

    if len(parts) >= 2 and callable(complete_json) and llm_planning_enabled():
        try:
            parsed = complete_json(f"Question: {question}", system=_PLANNER_SYSTEM)
            raw = parsed.get("subtasks") if isinstance(parsed, dict) else None
            tasks = validate_plan_tasks(
                raw, providers=providers, max_tasks=max_tasks, min_tasks=2
            )
            return ResearchPlan(question, tasks, "model")
        except Exception as exc:  # noqa: BLE001 — any planner failure falls back
            logger.warning("Model planning rejected (%s); using deterministic plan", exc)

    if parts:
        raw_tasks = [{"query": part, "purpose": "Focused sub-question"} for part in parts]
    else:
        raw_tasks = [
            {"query": question[:MAX_TASK_QUERY_CHARS], "purpose": "Answer the question directly"}
        ]
    tasks = validate_plan_tasks(raw_tasks, providers=providers, max_tasks=max_tasks)
    return ResearchPlan(question, tasks, "deterministic_fallback")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)


def _clean_doi(doi: str) -> str:
    value = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def _clean_arxiv(arxiv_id: str) -> str:
    value = (arxiv_id or "").strip().lower()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return _ARXIV_VERSION.sub("", value)


@dataclass
class SourceRecord:
    source_id: str
    provider: str
    source_kind: str  # "academic" | "web"
    title: str
    url: str
    retrieved_at: str
    access_status: str = "unavailable"
    text: str = ""
    task_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    excerpts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def doi(self) -> str:
        return str(self.metadata.get("doi") or "")

    @property
    def arxiv_id(self) -> str:
        return str(self.metadata.get("arxiv_id") or "")

    def identity_keys(self) -> List[str]:
        keys: List[str] = []
        if self.doi:
            keys.append(f"doi:{_clean_doi(self.doi)}")
        if self.arxiv_id:
            keys.append(f"arxiv:{_clean_arxiv(self.arxiv_id)}")
        url_key = canonical_url(self.url)
        if url_key:
            keys.append(f"url:{url_key}")
        title_key = _norm(self.title)
        if len(title_key) >= _MIN_TITLE_KEY_CHARS:
            keys.append("title:" + hashlib.sha1(title_key.encode("utf-8")).hexdigest()[:16])
        return keys

    def refresh_id(self) -> None:
        keys = self.identity_keys()
        if keys:
            self.source_id = keys[0]

    def to_public_dict(self) -> Dict[str, Any]:
        """Source metadata for run records; never includes page bodies."""
        public_meta = {
            key: value
            for key, value in self.metadata.items()
            if key in {"doi", "arxiv_id", "year", "authors", "snippet", "fetch_status",
                       "fetch_error", "final_url", "providers", "transport"}
        }
        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "source_kind": self.source_kind,
            "title": self.title,
            "url": self.url,
            "retrieved_at": self.retrieved_at,
            "access_status": self.access_status,
            "task_ids": list(self.task_ids),
            "text_chars": len(self.text),
            "excerpt_count": len(self.excerpts),
            "metadata": public_meta,
            "node_ids": list(self.metadata.get("node_ids") or []),
        }

    def to_reader_dict(self) -> Dict[str, Any]:
        """Paper-shaped dict consumed by :class:`ReaderAgent`."""
        data: Dict[str, Any] = {
            "title": self.title,
            "authors": list(self.metadata.get("authors") or []),
            "abstract": self.text if self.access_status == "abstract_only" else "",
            "year": self.metadata.get("year"),
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "url": self.url,
            "source": self.provider,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "access_status": self.access_status,
            "retrieved_at": self.retrieved_at,
            "task_ids": list(self.task_ids),
        }
        if self.excerpts:
            data["claims"] = [dict(item) for item in self.excerpts]
        return data


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_from_paper(paper: Any, provider: str, task_id: str) -> SourceRecord:
    abstract = str(getattr(paper, "abstract", "") or "")
    metadata: Dict[str, Any] = {
        "authors": list(getattr(paper, "authors", None) or []),
        "year": getattr(paper, "year", None),
        "doi": getattr(paper, "doi", "") or "",
        "arxiv_id": getattr(paper, "arxiv_id", "") or "",
        "providers": [provider],
    }
    extra = getattr(paper, "metadata", None) or {}
    if isinstance(extra, dict) and extra.get("transport"):
        metadata["transport"] = extra["transport"]
    record = SourceRecord(
        source_id="",
        provider=str(getattr(paper, "source", "") or provider),
        source_kind="academic",
        title=str(getattr(paper, "title", "") or ""),
        url=str(getattr(paper, "url", "") or ""),
        retrieved_at=_now_iso(),
        access_status="abstract_only" if abstract.strip() else "unavailable",
        text=abstract,
        task_ids=[task_id],
        metadata=metadata,
    )
    record.refresh_id()
    if not record.source_id:
        record.source_id = "title:" + hashlib.sha1(record.title.encode("utf-8")).hexdigest()[:16]
    return record


def record_from_web(hit: Any, task_id: str) -> SourceRecord:
    record = SourceRecord(
        source_id="",
        provider=WEB_PROVIDER,
        source_kind="web",
        title=str(getattr(hit, "title", "") or ""),
        url=str(getattr(hit, "url", "") or ""),
        retrieved_at=_now_iso(),
        access_status="unavailable",
        text="",
        task_ids=[task_id],
        metadata={"snippet": str(getattr(hit, "snippet", "") or ""), "providers": [WEB_PROVIDER]},
    )
    record.refresh_id()
    return record


def merge_records(
    ordered: Iterable[SourceRecord], limit: Optional[int] = None
) -> List[SourceRecord]:
    """Deduplicate in the given order by DOI, arXiv ID, canonical URL, then title.

    The first record wins; later duplicates only contribute missing identifiers
    and their task/provider attribution.
    """
    merged: List[SourceRecord] = []
    index: Dict[str, SourceRecord] = {}
    for record in ordered:
        keys = record.identity_keys()
        existing = next((index[k] for k in keys if k in index), None)
        if existing is None:
            merged.append(record)
            target = record
        else:
            target = existing
            for task_id in record.task_ids:
                if task_id not in target.task_ids:
                    target.task_ids.append(task_id)
            providers = target.metadata.setdefault("providers", [target.provider])
            for provider in record.metadata.get("providers") or [record.provider]:
                if provider not in providers:
                    providers.append(provider)
            for name in ("doi", "arxiv_id", "year", "authors"):
                if not target.metadata.get(name) and record.metadata.get(name):
                    target.metadata[name] = record.metadata[name]
            if not target.url and record.url:
                target.url = record.url
            if not target.text and record.text and record.access_status == "abstract_only":
                target.text, target.access_status = record.text, record.access_status
        target.refresh_id()
        for key in target.identity_keys():
            index.setdefault(key, target)
    return merged[:limit] if limit else merged


def extract_excerpts(
    text: str, query: str, *, max_excerpts: int = 3, max_chars: int = 500
) -> List[Dict[str, Any]]:
    """Pick the sentences of *text* that best match *query*, kept verbatim.

    ``span_start``/``span_end`` index into *text*, so ``text[start:end]`` is the
    excerpt exactly.
    """
    wanted = set(_tokens(query))
    if not text or not wanted:
        return []
    scored: List[Tuple[float, int, int, int, List[str]]] = []
    for match in re.finditer(r"[^\n.!?]+(?:[.!?]+|\n|$)", text):
        raw = match.group(0)
        sentence = raw.strip()
        if len(sentence) < 40:
            continue
        start = match.start() + (len(raw) - len(raw.lstrip()))
        end = start + min(len(sentence), max_chars)
        overlap = sorted(wanted & set(_tokens(sentence)))
        if not overlap:
            continue
        score = len(overlap) / math.sqrt(max(len(sentence.split()), 1))
        scored.append((score, start, end, len(overlap), overlap))
    best = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_excerpts]
    excerpts = []
    for score, start, end, hits, overlap in sorted(best, key=lambda item: item[1]):
        excerpts.append(
            {
                "type": "claim",
                "text": text[start:end],
                "confidence": round(min(0.7, 0.3 + 0.1 * hits), 2),
                "tags": overlap[:5],
                "span_start": start,
                "span_end": end,
                "extraction_method": "sentence_overlap",
                "evidence_kind": "excerpt",
            }
        )
    return excerpts


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@dataclass
class ProviderRun:
    provider: str
    status: str  # ok | empty | partial | error | skipped
    records: List[SourceRecord] = field(default_factory=list)
    error: str = ""
    elapsed_ms: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "result_count": len(self.records),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "error": self.error,
            **self.detail,
        }


@dataclass
class _TaskRun:
    task: ResearchTask
    index: int
    runs: List[ProviderRun] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    started: bool = False
    finished: bool = False
    closed: bool = False
    reason: str = ""

@dataclass
class ExecutionResult:
    tasks: List[Dict[str, Any]]
    sources: List[SourceRecord]
    events: List[Dict[str, Any]]
    external_search: Dict[str, Any]
    errors: List[str]
    degraded: bool


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _task_status(task_run: "_TaskRun") -> str:
    runs = task_run.runs
    if not task_run.finished:
        return "failed" if task_run.started else "skipped"
    active = [r for r in runs if r.status != "skipped"]
    if not active:
        return "skipped"
    if all(r.status == "error" for r in active):
        return "failed"
    if any(r.status in {"error", "partial"} for r in active):
        return "partial"
    return "completed"


def aggregate_status(runs: List[ProviderRun]) -> str:
    active = [r.status for r in runs if r.status != "skipped"]
    if not active:
        return "skipped"
    if all(s == "error" for s in active):
        return "error"
    if any(s in {"error", "partial"} for s in active):
        return "partial"
    if "ok" in active:
        return "ok"
    return "empty"


class ResearchTaskExecutor:
    """Run planned subtasks concurrently and merge their evidence."""

    def __init__(
        self,
        *,
        run_id: str,
        literature_search: Any = None,
        web_search: Any = None,
        source_fetch: Any = None,
        limits: Optional[ResearchLimits] = None,
    ) -> None:
        self.run_id = run_id
        self.literature_search = literature_search
        self.web_search = web_search
        self.source_fetch = source_fetch
        self.limits = limits or ResearchLimits()
        self._sink: Optional[EventSink] = None
        self._cancel: Optional[threading.Event] = None
        self._stop_at = float("inf")
    # -- public API ------------------------------------------------------
    def available_providers(self) -> Tuple[str, ...]:
        providers: List[str] = []
        if self.literature_search is not None:
            providers.extend(ACADEMIC_PROVIDERS)
        if self.web_search is not None and getattr(self.web_search, "enabled", False):
            providers.append(WEB_PROVIDER)
        return tuple(providers)

    def execute(
        self,
        plan: ResearchPlan,
        *,
        cancel_event: Optional[threading.Event] = None,
        deadline: Optional[float] = None,
        on_event: Optional[EventSink] = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        stop_at = started + self.limits.max_research_s
        if deadline is not None:
            stop_at = min(stop_at, deadline)
        self._sink = on_event
        self._cancel = cancel_event
        self._stop_at = stop_at

        runs = self._fan_out(plan)
        ordered: List[SourceRecord] = []
        for task_run in runs:
            for provider_run in task_run.runs:
                ordered.extend(provider_run.records)
        sources = merge_records(ordered, self.limits.max_merged_sources)

        fetch_events = self._fetch_pages(sources, plan)
        for source in sources:
            source.refresh_id()

        events: List[Dict[str, Any]] = []
        for task_run in runs:
            events.extend(task_run.events)
        events.extend(fetch_events)

        return self._summarize(plan, runs, sources, events)

    # -- fan-out ---------------------------------------------------------
    def _stopped(self) -> Optional[str]:
        if self._cancel is not None and self._cancel.is_set():
            return "cancelled"
        if time.monotonic() >= self._stop_at:
            return "research time budget exhausted"
        return None

    def _emit(self, bucket: List[Dict[str, Any]], **kwargs: Any) -> None:
        event = make_task_event(run_id=self.run_id, **kwargs)
        bucket.append(event)
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:  # noqa: BLE001 — listeners must not break workers
                logger.exception("Task event listener failed")

    def _fan_out(self, plan: ResearchPlan) -> List[_TaskRun]:
        task_runs = [_TaskRun(task=t, index=i) for i, t in enumerate(plan.subtasks)]
        pool = ThreadPoolExecutor(
            max_workers=max(1, self.limits.max_workers), thread_name_prefix="research-task"
        )
        futures: Dict[Future, _TaskRun] = {
            pool.submit(self._run_task, task_run): task_run for task_run in task_runs
        }
        pending = set(futures)
        try:
            while pending:
                reason = self._stopped()
                if reason:
                    break
                done, pending = wait(pending, timeout=0.1)
                for future in done:
                    error = future.exception()
                    if error is not None:
                        task_run = futures[future]
                        logger.error("Research task %s crashed: %s", task_run.task.task_id, error)
                        task_run.runs.append(
                            ProviderRun("worker", "error", error=f"{type(error).__name__}: {error}")
                        )
                        task_run.finished = True
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        reason = self._stopped() or "research time budget exhausted"
        for future in pending:
            task_run = futures[future]
            if task_run.finished:
                continue
            task_run.closed = True
            task_run.reason = reason
            self._emit(
                task_run.events,
                kind="task_failed" if task_run.started else "task_skipped",
                task_id=task_run.task.task_id,
                message=reason,
                data={"reason": reason},
            )
        return task_runs

    def _run_task(self, task_run: _TaskRun) -> None:
        task = task_run.task
        started = time.monotonic()
        if task_run.closed:
            return
        reason = self._stopped()
        if reason:
            self._emit(
                task_run.events, kind="task_skipped", task_id=task.task_id,
                message=reason, data={"reason": reason},
            )
            task_run.reason = reason
            task_run.finished = True
            return
        task_run.started = True
        self._emit(
            task_run.events,
            kind="task_started",
            task_id=task.task_id,
            message=task.query,
            data={"query": task.query, "providers": list(task.providers)},
        )
        for name in self._provider_names(task):
            if task_run.closed:
                return
            reason = self._stopped()
            if reason:
                task_run.runs.append(ProviderRun(name, "skipped", error=reason))
                self._emit(
                    task_run.events, kind="task_skipped", task_id=task.task_id,
                    provider=name, message=reason, data={"reason": reason},
                )
                continue
            call_started = time.monotonic()
            try:
                run = self._call_provider(name, task)
            except Exception as exc:  # noqa: BLE001 — a failed provider must not hide siblings
                run = ProviderRun(name, "error", error=f"{type(exc).__name__}: {exc}"[:500])
            if task_run.closed:
                return
            run.elapsed_ms = run.elapsed_ms or (time.monotonic() - call_started) * 1000.0
            task_run.runs.append(run)
            kind = {"error": "task_failed", "skipped": "task_skipped"}.get(run.status, "task_completed")
            self._emit(
                task_run.events, kind=kind, task_id=task.task_id, provider=name,
                status=run.status, message=run.error,
                data={"result_count": len(run.records), "elapsed_ms": round(run.elapsed_ms, 3)},
            )
        task_run.elapsed_ms = (time.monotonic() - started) * 1000.0
        task_run.finished = True

    def _provider_names(self, task: ResearchTask) -> List[str]:
        names: List[str] = []
        academic = [p for p in ACADEMIC_PROVIDERS if p in task.providers]
        skill = self.literature_search
        if academic and skill is not None:
            search = getattr(skill, "search_with_outcome", None)
            if callable(search) and _accepts(search, "provider"):
                names.extend(academic)
            else:
                names.append("academic")
        if WEB_PROVIDER in task.providers:
            names.append(WEB_PROVIDER)
        return names

    def _call_provider(self, name: str, task: ResearchTask) -> ProviderRun:
        if name == WEB_PROVIDER:
            return self._search_web(task)
        return self._search_academic(name, task)

    def _search_academic(self, name: str, task: ResearchTask) -> ProviderRun:
        guard = allow_tool("literature_search")
        if not guard.allowed:
            return ProviderRun(name, "error", error=guard.reason)
        skill = self.literature_search
        limit = self.limits.results_per_provider
        search = getattr(skill, "search_with_outcome", None)
        if callable(search):
            kwargs: Dict[str, Any] = {"query": task.query, "max_results": limit}
            if name != "academic":
                kwargs["provider"] = name
            outcome = search(**kwargs)
            papers = list(outcome.results)[:limit]
            status = outcome.status.value if isinstance(outcome.status, SearchStatus) else str(outcome.status)
            errors = getattr(outcome, "errors", {}) or {}
            transports = sorted(
                {
                    str(getattr(o, "transport", ""))
                    for o in getattr(outcome, "outcomes", [])
                    if getattr(o, "transport", "")
                }
            )
            run = ProviderRun(
                name,
                status,
                error="; ".join(f"{k}: {v}" for k, v in errors.items()),
                elapsed_ms=float(getattr(outcome, "elapsed_ms", 0.0) or 0.0),
                detail={"transport": ",".join(transports)} if transports else {},
            )
        else:
            papers = list(skill.search(query=task.query, max_results=limit))[:limit]
            run = ProviderRun(name, "ok" if papers else "empty", detail={"transport": "custom"})
        run.records = [record_from_paper(p, name, task.task_id) for p in papers]
        for record in run.records:
            if run.detail.get("transport"):
                record.metadata.setdefault("transport", run.detail["transport"])
        return run

    def _search_web(self, task: ResearchTask) -> ProviderRun:
        guard = allow_tool("web_search")
        if not guard.allowed:
            return ProviderRun(WEB_PROVIDER, "error", error=guard.reason)
        skill = self.web_search
        if skill is None or not getattr(skill, "enabled", False):
            return ProviderRun(WEB_PROVIDER, "skipped", error="web search disabled")
        outcome = skill.search_with_outcome(task.query, self.limits.results_per_provider)
        hits = list(outcome.results)[: self.limits.results_per_provider]
        run = ProviderRun(
            WEB_PROVIDER,
            outcome.status.value,
            error=getattr(outcome, "error", "") or "",
            elapsed_ms=float(getattr(outcome, "elapsed_ms", 0.0) or 0.0),
        )
        run.records = [record_from_web(hit, task.task_id) for hit in hits]
        return run

    # -- page fetching ---------------------------------------------------
    def _fetch_pages(self, sources: List[SourceRecord], plan: ResearchPlan) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        web = [s for s in sources if s.source_kind == "web" and s.url]
        if not web:
            return events
        queries = {t.task_id: t.query for t in plan.subtasks}

        guard = allow_tool("fetch_source")
        fetcher = self.source_fetch
        if fetcher is None or not guard.allowed:
            for source in web:
                source.metadata["fetch_status"] = "skipped"
                source.metadata["fetch_error"] = "page fetching unavailable"
            return events

        budget = self.limits.max_fetched_pages
        chosen, skipped = web[:budget], web[budget:]
        for source in skipped:
            source.metadata["fetch_status"] = "skipped"
            source.metadata["fetch_error"] = "page budget reached"

        results: Dict[int, Any] = {}
        buckets: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(chosen))}

        def work(i: int, source: SourceRecord) -> None:
            reason = self._stopped()
            task_id = source.task_ids[0] if source.task_ids else ""
            if reason:
                results[i] = ("skipped", reason)
                self._emit(
                    buckets[i], kind="task_skipped", task_id=task_id,
                    provider="fetch_source", message=reason, data={"url": source.url},
                )
                return
            outcome = fetcher.fetch(source.url)
            results[i] = ("done", outcome)
            ok = outcome.status is FetchStatus.OK
            self._emit(
                buckets[i], kind="task_completed" if ok else "task_failed", task_id=task_id,
                provider="fetch_source", status="ok" if ok else outcome.status.value,
                message="" if ok else outcome.error,
                data={"url": source.url, "elapsed_ms": round(outcome.elapsed_ms, 3),
                      "text_chars": len(outcome.text)},
            )

        pool = ThreadPoolExecutor(
            max_workers=max(1, self.limits.max_workers), thread_name_prefix="research-fetch"
        )
        futures = [pool.submit(work, i, s) for i, s in enumerate(chosen)]
        try:
            remaining = max(0.0, self._stop_at - time.monotonic())
            wait(futures, timeout=remaining + 1.0)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        for i, source in enumerate(chosen):
            events.extend(buckets[i])
            state = results.get(i)
            if state is None or state[0] == "skipped":
                source.metadata["fetch_status"] = "skipped"
                source.metadata["fetch_error"] = state[1] if state else "fetch did not finish"
                continue
            outcome = state[1]
            source.metadata["fetch_status"] = outcome.status.value
            if outcome.status is FetchStatus.OK:
                source.text = outcome.text
                source.access_status = "full_text"
                source.metadata["final_url"] = outcome.final_url
                if outcome.title and not source.title:
                    source.title = outcome.title
                query = " ".join(queries.get(tid, "") for tid in source.task_ids) or plan.primary_question
                source.excerpts = extract_excerpts(outcome.text, query)
            else:
                source.access_status = "failed"
                source.metadata["fetch_error"] = outcome.error
        return events

    # -- fan-in ----------------------------------------------------------
    def _summarize(
        self,
        plan: ResearchPlan,
        runs: List[_TaskRun],
        sources: List[SourceRecord],
        events: List[Dict[str, Any]],
    ) -> ExecutionResult:
        by_task: Dict[str, List[str]] = {}
        for source in sources:
            for task_id in source.task_ids:
                by_task.setdefault(task_id, []).append(source.source_id)

        tasks: List[Dict[str, Any]] = []
        all_runs: List[ProviderRun] = []
        errors: Dict[str, str] = {}
        for task_run in runs:
            all_runs.extend(task_run.runs)
            for provider_run in task_run.runs:
                if provider_run.status in {"error", "partial"} and provider_run.error:
                    errors.setdefault(provider_run.provider, provider_run.error)
            tasks.append(
                {
                    **task_run.task.to_dict(),
                    "status": _task_status(task_run),
                    "provider_results": [r.to_dict() for r in task_run.runs],
                    "result_count": sum(len(r.records) for r in task_run.runs),
                    "source_ids": by_task.get(task_run.task.task_id, []),
                    "elapsed_ms": round(task_run.elapsed_ms, 3),
                    **({"reason": task_run.reason} if task_run.reason else {}),
                }
            )

        status = aggregate_status(all_runs)
        by_provider: Dict[str, Dict[str, Any]] = {}
        for provider_run in all_runs:
            entry = by_provider.setdefault(
                provider_run.provider, {"provider": provider_run.provider, "calls": 0, "result_count": 0, "statuses": []}
            )
            entry["calls"] += 1
            entry["result_count"] += len(provider_run.records)
            entry["statuses"].append(provider_run.status)
        external_search = {
            "status": status,
            "provider": "multi",
            "result_count": len(sources),
            "elapsed_ms": round(sum(t["elapsed_ms"] for t in tasks), 3),
            "providers": by_provider,
            "errors": errors,
        }
        error_messages: List[str] = []
        if status == "error":
            detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "all providers failed"
            error_messages.append(f"External search failed ({detail}).")
        return ExecutionResult(
            tasks=tasks,
            sources=sources,
            events=events,
            external_search=external_search,
            errors=error_messages,
            degraded=status in {"error", "partial"},
        )

    @staticmethod
    def skipped_result(
        plan: ResearchPlan, run_id: str, reason: str, *, status: str = "skipped"
    ) -> ExecutionResult:
        """Result for runs that never leave project memory."""
        events: List[Dict[str, Any]] = []
        tasks: List[Dict[str, Any]] = []
        for task in plan.subtasks:
            events.append(
                make_task_event(
                    run_id=run_id, kind="task_skipped", task_id=task.task_id,
                    message=reason, data={"reason": reason},
                )
            )
            tasks.append(
                {**task.to_dict(), "status": "skipped", "provider_results": [],
                 "result_count": 0, "source_ids": [], "elapsed_ms": 0.0, "reason": reason}
            )
        return ExecutionResult(
            tasks=tasks,
            sources=[],
            events=events,
            external_search={
                "status": status, "provider": "none", "result_count": 0, "errors": {},
            },
            errors=[],
            degraded=False,
        )
