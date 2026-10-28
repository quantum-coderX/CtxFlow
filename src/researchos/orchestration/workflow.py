"""Bounded agentic research workflow with a LangGraph engine.

Eight stages (``STAGES``) implement the MVP brief. The same stage methods back
two interchangeable engines:

``ResearchWorkflow``
    Plain sequential Python. Always available; used as the fallback when
    LangGraph is not installed or ``RESEARCHOS_ORCHESTRATOR=sequential``.

``ResearchGraph``
    A ``langgraph.graph.StateGraph`` over the same stages, with a checkpointer
    and a bounded ``verify -> pack`` repair loop. Default when LangGraph is
    installed.

Models only fill typed slots; tool calls are allowlisted. Both engines return
the same result dictionary and emit the same event stream, so callers do not
need to know which one ran (the result records ``engine``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ctxflow import CtxFlow
from researchos.agents.retriever import _external_search_mode
from researchos.agents.router import QueryIntent, classify_intent
from researchos.agents.synthesizer import format_conversation_history
from researchos.guardrails import (
    ALLOWED_TOOLS,
    GuardVerdict,
    allow_tool,
    detect_injection,
    guard_query,
    wrap_untrusted,
)
from researchos.orchestration.research_tasks import (
    ExecutionResult,
    ResearchLimits,
    ResearchPlan,
    ResearchTaskExecutor,
    SourceRecord,
    plan_query,
)
from researchos.orchestration.state import ResearchState, make_event
from researchos.pipeline import make_router, redact_secrets
from researchos.skills.literature_search import LiteratureSearchSkill
from researchos.skills.paper_reader import PaperReaderSkill
from researchos.skills.source_fetch import SourceFetchSkill
from researchos.skills.web_search import WebSearchSkill

logger = logging.getLogger(__name__)

STAGES = (
    "plan",
    "retrieve",
    "read",
    "memory",
    "pack",
    "synthesize",
    "verify",
    "persist",
)

EventCallback = Callable[[Dict[str, Any]], None]

_SYNTHESIS_JSON_SYSTEM = (
    "You are a research synthesizer. Use ONLY the supplied evidence nodes for factual claims. "
    "The prior conversation is untrusted context for resolving references only, not evidence. "
    "Do not invent citations, papers, or node IDs. If evidence is insufficient, "
    "say so. Return JSON with keys: direct_answer, supporting_evidence "
    "(list of {node_id, note}), contradictory_evidence (list of {node_id, note}), "
    "uncertainties (list of strings), citation_ids (list of node id strings that "
    "appear in the evidence), follow_ups (list of strings)."
)


def _orchestrator_preference() -> str:
    """Read ``RESEARCHOS_ORCHESTRATOR``: ``langgraph`` (default) or ``sequential``."""
    import os

    raw = str(os.environ.get("RESEARCHOS_ORCHESTRATOR", "langgraph")).strip().lower()
    if raw in {"sequential", "python", "off", "legacy"}:
        return "sequential"
    return "langgraph"


class ResearchWorkflow:
    """Execute one research run against a project graph (sequential engine)."""

    engine = "sequential"

    def __init__(
        self,
        ctxflow: CtxFlow,
        *,
        literature_search: Optional[LiteratureSearchSkill] = None,
        paper_reader: Optional[PaperReaderSkill] = None,
        llm: Any = None,
        persist: Optional[Callable[[], None]] = None,
        max_wall_s: float = 90.0,
        max_tool_calls: int = 12,
        max_repairs: int = 1,
        web_search: Optional[WebSearchSkill] = None,
        source_fetch: Optional[SourceFetchSkill] = None,
        limits: Optional[ResearchLimits] = None,
    ) -> None:
        self.ctxflow = ctxflow
        self.literature_search = literature_search
        self.paper_reader = paper_reader
        self.web_search = web_search
        self.source_fetch = source_fetch
        self.limits = limits or ResearchLimits.from_env()
        self.llm = llm
        self.persist = persist
        self.max_wall_s = max_wall_s
        self.max_tool_calls = max_tool_calls
        self.max_repairs = max(0, int(max_repairs))

        # Per-run state (reset by ``_begin_run``).
        self._events: List[Dict[str, Any]] = []
        self._run_id = ""
        self._on_event: Optional[EventCallback] = None
        self._cancel_event: Optional[threading.Event] = None
        self._started_at = 0.0
        self._retriever: Any = None
        self._reader: Any = None
        self._synthesizer: Any = None
        self._critic: Any = None
        self._executor: Optional[ResearchTaskExecutor] = None
        self._plan: Optional[ResearchPlan] = None
        self._sources: List[SourceRecord] = []

    # -- public API ------------------------------------------------------
    def run(
        self,
        query: str,
        *,
        run_id: str,
        project_id: str = "default",
        session_id: str = "default",
        conversation_history: Optional[List[Dict[str, str]]] = None,
        token_budget: int = 2048,
        on_event: Optional[EventCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        self._begin_run(run_id, on_event, cancel_event)
        state = self._initial_state(
            query,
            run_id,
            project_id,
            session_id,
            conversation_history or [],
            token_budget,
        )

        try:
            for stage in (
                self._stage_plan,
                self._stage_retrieve,
                self._stage_read,
                self._stage_memory,
            ):
                stage(state)
                if state.get("stop"):
                    break

            if not state.get("stop"):
                while True:
                    self._stage_pack(state)
                    if state.get("stop"):
                        break
                    self._stage_synthesize(state)
                    if state.get("stop"):
                        break
                    self._stage_verify(state)
                    if state.get("stop") or not state.get("needs_repair"):
                        break
                if not state.get("stop"):
                    self._stage_persist(state)
        except Exception as exc:  # noqa: BLE001 — surface as a failed run, never fake success
            logger.exception("Research workflow failed")
            state["errors"] = list(state.get("errors") or []) + [str(exc)]
            state["degraded"] = True
            self._emit("persist", "failed", str(exc))
            return self._finalize(state, self._events, success=False, status="failed")

        success, status = self._status_from_state(state)
        return self._finalize(state, self._events, success=success, status=status)

    # -- run setup -------------------------------------------------------
    def _begin_run(
        self,
        run_id: str,
        on_event: Optional[EventCallback],
        cancel_event: Optional[threading.Event],
    ) -> None:
        self._run_id = run_id
        self._on_event = on_event
        self._cancel_event = cancel_event
        self._events = []
        self._started_at = time.monotonic()
        self._retriever = None
        self._reader = None
        self._synthesizer = None
        self._critic = None
        self._executor = None
        self._plan = None
        self._sources = []

    def _initial_state(
        self,
        query: str,
        run_id: str,
        project_id: str,
        session_id: str,
        conversation_history: List[Dict[str, str]],
        token_budget: int,
    ) -> ResearchState:
        return {
            "run_id": run_id,
            "project_id": project_id,
            "session_id": session_id,
            "query": query,
            "conversation_history": conversation_history,
            "token_budget": max(1, int(token_budget)),
            "plan": list(STAGES),
            "research_plan": {},
            "tasks": [],
            "sources": [],
            "citations": [],
            "fresh_sources": False,
            "allowed_tools": sorted(ALLOWED_TOOLS),
            "alerts": [],
            "errors": [],
            "cited_nodes": [],
            "packed_node_ids": [],
            "excluded_node_ids": [],
            "ingested_papers": [],
            "internal_results": [],
            "external_results": [],
            "degraded": False,
            "cancelled": False,
            "verdict": "",
            "answer": "",
            "needs_repair": False,
            "repairs": 0,
            "max_repairs": self.max_repairs,
            "stop": False,
            "stop_status": "",
            "blocked": False,
            "engine": self.engine,
        }

    def _emit(
        self,
        stage: str,
        status: str,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = make_event(
            run_id=self._run_id,
            stage=stage,
            status=status,
            message=message,
            data=redact_secrets(data or {}),
        )
        self._events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 — a listener must not fail the run
                logger.exception("Event callback failed")

    def _publish(self, event: Dict[str, Any]) -> None:
        """Forward a live event to the listener without recording it."""
        if self._on_event is None:
            return
        try:
            self._on_event(redact_secrets(event))
        except Exception:  # noqa: BLE001 — a listener must not fail the run
            logger.exception("Event callback failed")

    def _executor_for_run(self) -> ResearchTaskExecutor:
        if self._executor is None:
            self._executor = ResearchTaskExecutor(
                run_id=self._run_id,
                literature_search=self.literature_search,
                web_search=self.web_search,
                source_fetch=self.source_fetch,
                limits=self.limits,
            )
        return self._executor

    def _ensure_agents(self, state: ResearchState) -> None:
        if self._retriever is not None:
            return
        router = make_router(
            self.ctxflow,
            literature_search=self.literature_search,
            paper_reader=self.paper_reader,
            llm=self.llm,
            token_budget=state["token_budget"],
        )
        self._retriever = router._agents["retriever"]
        self._reader = router._agents["reader"]
        self._synthesizer = router._agents["synthesizer"]
        self._critic = router._agents["critic"]

    # -- stop conditions -------------------------------------------------
    def _maybe_stop(self, state: ResearchState) -> bool:
        """Stop the run on cancellation or wall-clock exhaustion."""
        if state.get("stop"):
            return True
        if self._cancelled(self._cancel_event, state, self._emit):
            state["stop"] = True
            state["stop_status"] = "cancelled"
            return True
        if self._timed_out(self._started_at):
            state["errors"] = list(state.get("errors") or []) + [
                f"wall-clock budget of {self.max_wall_s:.0f}s exceeded"
            ]
            state["degraded"] = True
            state["stop"] = True
            state["stop_status"] = "failed"
            self._emit("persist", "failed", "Wall-clock budget exceeded.")
            return True
        return False

    # -- stage A: plan ---------------------------------------------------
    def _stage_plan(self, state: ResearchState) -> ResearchState:
        guard: GuardVerdict = guard_query(state["query"])
        state["guard"] = guard.to_dict()
        if not guard.allowed:
            self._emit("plan", "blocked", guard.reason, guard.to_dict())
            state["verdict"] = "INSUFFICIENT_EVIDENCE"
            state["answer"] = guard.reason
            state["alerts"] = [
                {"type": "guardrail", "message": guard.reason, **guard.to_dict()}
            ]
            state["blocked"] = True
            state["stop"] = True
            state["stop_status"] = "failed"
            return state

        self._emit("plan", "started", "Classifying intent and building a bounded plan.")
        intent = classify_intent(state["query"])
        state["intent"] = intent.value
        need_external = intent in (
            QueryIntent.NEW_LITERATURE,
            QueryIntent.UNKNOWN,
            QueryIntent.CLAIM_VERIFICATION,
        )
        state["need_external"] = need_external
        state["fresh_sources"] = intent is QueryIntent.NEW_LITERATURE
        self._ensure_agents(state)

        providers = (
            ()
            if _external_search_mode() == "off"
            else self._executor_for_run().available_providers()
        )
        plan = plan_query(
            state["query"],
            providers=providers,
            llm=self.llm,
            max_tasks=self.limits.max_subtasks,
        )
        self._plan = plan
        state["research_plan"] = plan.to_dict()
        state["tasks"] = [
            {**task.to_dict(), "status": "pending", "provider_results": [], "result_count": 0}
            for task in plan.subtasks
        ]
        self._emit(
            "plan",
            "completed",
            f"Intent {intent.value}; {len(plan.subtasks)} research subtask(s) "
            f"({plan.planner}); external retrieval "
            f"{'enabled' if need_external else 'only if memory is insufficient'}.",
            {
                "intent": intent.value,
                "plan": list(STAGES),
                "research_plan": state["research_plan"],
                "limits": self.limits.to_dict(),
                "allowed_tools": state["allowed_tools"],
                "engine": self.engine,
            },
        )
        self._maybe_stop(state)
        return state

    # -- stage B: retrieve -----------------------------------------------
    def _stage_retrieve(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("retrieve", "started", "Querying project memory.")
        tool_guard = allow_tool("graph_query")
        if not tool_guard.allowed:
            raise RuntimeError(tool_guard.reason)

        query = state["query"]
        retrieve_result = self._retriever.run(
            query=query, max_results=8, skip_external=True
        )
        retrieve_data = retrieve_result.data or {}
        state["internal_results"] = retrieve_data.get("internal_results") or []
        knowledge_gap = bool(retrieve_data.get("knowledge_gap"))
        state["knowledge_gap"] = knowledge_gap
        external_search = retrieve_data.get("external_search")

        execution = self._run_research_tasks(state, knowledge_gap, external_search)
        external_search = execution.external_search
        self._events.extend(execution.events)
        self._sources = list(execution.sources)
        state["tasks"] = execution.tasks
        state["sources"] = [source.to_public_dict() for source in execution.sources]
        state["external_results"] = [
            source.to_reader_dict()
            for source in execution.sources
            if self._has_evidence(source)
        ]
        state["external_search"] = external_search
        if execution.errors:
            state["errors"] = list(state.get("errors") or []) + execution.errors
        if execution.degraded:
            state["degraded"] = True
        if not retrieve_result.success:
            state["degraded"] = True
            state["errors"] = list(state.get("errors") or []) + [
                retrieve_result.error or "retriever failed"
            ]
        self._emit(
            "retrieve",
            "completed"
            if retrieve_result.success and not execution.degraded
            else "degraded",
            "Memory search finished.",
            {
                "knowledge_gap": knowledge_gap,
                "internal_count": len(state["internal_results"]),
                "external_count": len(state["external_results"]),
                "external_search": external_search,
                "tasks": [self._task_brief(task) for task in execution.tasks],
                "sources": [
                    {k: s[k] for k in ("source_id", "title", "url", "provider", "source_kind", "access_status")}
                    for s in state["sources"]
                ],
            },
        )
        self._maybe_stop(state)
        return state

    @staticmethod
    def _has_evidence(source: SourceRecord) -> bool:
        """Only sources with real text enter the graph; snippets never do."""
        if source.source_kind == "web":
            return source.access_status == "full_text" and bool(source.excerpts)
        return True

    @staticmethod
    def _task_brief(task: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "task_id": task.get("task_id"),
            "query": task.get("query"),
            "status": task.get("status"),
            "result_count": task.get("result_count", 0),
            "elapsed_ms": task.get("elapsed_ms", 0.0),
            "providers": [
                {k: r.get(k) for k in ("provider", "status", "result_count", "elapsed_ms")}
                for r in task.get("provider_results") or []
            ],
        }

    def _run_research_tasks(
        self,
        state: ResearchState,
        knowledge_gap: bool,
        retriever_external: Optional[Dict[str, Any]],
    ) -> ExecutionResult:
        """Fan planned subtasks out to bounded workers, or skip with a reason."""
        plan = self._plan
        assert plan is not None  # set by the plan stage
        mode = _external_search_mode()
        executor = self._executor_for_run()
        fresh = bool(state.get("fresh_sources"))

        reason = ""
        if mode == "off":
            reason = "external search disabled"
        elif not (knowledge_gap or fresh):
            reason = "project memory has sufficient evidence"
        elif not executor.available_providers():
            reason = "no research providers configured"
        elif not allow_tool("literature_search").allowed:
            reason = "search tool not allowed"

        if reason:
            result = ResearchTaskExecutor.skipped_result(plan, self._run_id, reason)
            if retriever_external:
                result.external_search = retriever_external
            for event in result.events:
                self._publish(event)
            return result

        return executor.execute(
            plan,
            cancel_event=self._cancel_event,
            deadline=self._started_at + self.max_wall_s,
            on_event=self._publish,
        )

    # -- stage C: read ---------------------------------------------------
    def _stage_read(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("read", "started", "Extracting claims from retrieved papers.")
        if state["external_results"]:
            read_guard = allow_tool("paper_reader")
            if read_guard.allowed:
                read_result = self._reader.run(
                    query=state["query"],
                    context={
                        "previous_output": {
                            "internal_results": state["internal_results"],
                            "external_results": state["external_results"],
                            "knowledge_gap": state.get("knowledge_gap"),
                            "external_search": state.get("external_search"),
                            "fulltext_deadline": self._started_at + self.max_wall_s,
                        }
                    },
                    previous_results=[],
                )
                read_data = read_result.data or {}
                state["ingested_papers"] = read_data.get("ingested_papers") or []
                self._attach_node_ids(state)
                if not read_result.success:
                    # A paper that could not be read costs evidence but must
                    # not discard the graph answer; surface it as degradation.
                    state["degraded"] = True
                    state["errors"] = list(state.get("errors") or []) + [
                        read_result.error or "paper reader failed"
                    ]
                self._emit(
                    "read",
                    "completed" if read_result.success else "degraded",
                    f"Ingested {len(state['ingested_papers'])} paper(s).",
                    {
                        "ingested_papers": state["ingested_papers"],
                        "total_nodes_created": read_data.get("total_nodes_created", 0),
                    },
                )
            else:
                self._emit("read", "blocked", read_guard.reason, read_guard.to_dict())
        else:
            self._emit("read", "skipped", "No new papers to read; using project memory.")
        return state

    @staticmethod
    def _attach_node_ids(state: ResearchState) -> None:
        """Record which graph nodes each ingested source produced."""
        by_source = {
            item.get("source_id"): item
            for item in state.get("ingested_papers") or []
            if item.get("source_id")
        }
        for source in state.get("sources") or []:
            item = by_source.get(source.get("source_id"))
            if item is not None:
                source["node_ids"] = list(item.get("node_ids") or [])
                source["source_node_id"] = item.get("source_node_id", "")
                source["reused"] = bool(item.get("reused"))

    # -- stage D: memory -------------------------------------------------
    def _stage_memory(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("memory", "started", "Persisting project graph.")
        if self.persist is not None:
            self.persist()
        self._emit(
            "memory",
            "completed",
            "Project graph saved.",
            {
                "nodes": self.ctxflow.graph.node_count,
                "edges": self.ctxflow.graph.edge_count,
            },
        )
        self._maybe_stop(state)
        return state

    # -- stage E: pack ---------------------------------------------------
    def _stage_pack(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("pack", "started", "Packing evidence into the token budget.")
        synth_result = self._synthesizer.run(
            query=state["query"],
            context={
                "token_budget": state["token_budget"],
                "conversation_history": state.get("conversation_history") or [],
            },
        )
        synth_data = synth_result.data or {}
        packed_ids = list(synth_data.get("cited_nodes") or [])

        excluded = set(state.get("excluded_node_ids") or [])
        if excluded:
            filtered = [node_id for node_id in packed_ids if node_id not in excluded]
            if filtered:
                packed_ids = filtered

        state["packed_node_ids"] = packed_ids
        state["synthesis_data"] = {
            "answer": synth_data.get("answer") or "",
            "alerts": list(synth_data.get("alerts") or []),
        }
        state["synthesis_ok"] = bool(synth_result.success)
        state["synthesis_error"] = synth_result.error or ""

        usage = synth_result.metadata or {}
        retrieved_n = int(usage.get("nodes_retrieved") or 0)
        used_n = int(usage.get("nodes_used") or len(packed_ids))
        state["context_usage"] = {
            "nodes_retrieved": retrieved_n,
            "nodes_used": used_n,
            "nodes_dropped": int(
                usage.get("nodes_dropped") or max(0, retrieved_n - used_n)
            ),
            "tokens_used": usage.get("tokens_used"),
            "token_budget": usage.get("token_budget", state["token_budget"]),
            "selected_node_ids": packed_ids,
            "excluded_node_ids": sorted(excluded),
        }
        self._emit(
            "pack",
            "completed",
            "Context packer selected evidence nodes.",
            state["context_usage"],
        )
        self._maybe_stop(state)
        return state

    # -- stage F: synthesize ---------------------------------------------
    def _stage_synthesize(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("synthesize", "started", "Generating an evidence-grounded answer.")
        packed_ids = list(state.get("packed_node_ids") or [])
        synth_data = state.get("synthesis_data") or {}

        structured = self._maybe_structured_answer(
            state["query"],
            packed_ids,
            conversation_history=state.get("conversation_history") or [],
            token_budget=state["token_budget"],
        )
        if structured:
            state["structured"] = structured
            state["answer"] = self._format_structured(structured)
            cited = [
                item
                for item in structured.get("citation_ids") or packed_ids
                if isinstance(item, str)
            ]
            state["cited_nodes"] = cited or packed_ids
        else:
            state["answer"] = synth_data.get("answer") or ""
            state["cited_nodes"] = packed_ids
            if not packed_ids:
                state["answer"] = self._insufficient_answer(state) or state["answer"]
            if not state.get("synthesis_ok", True):
                state["degraded"] = True
                state["errors"] = list(state.get("errors") or []) + [
                    state.get("synthesis_error") or "synthesis failed"
                ]
        state["alerts"] = list(synth_data.get("alerts") or [])
        self._emit(
            "synthesize",
            "completed" if state.get("synthesis_ok", True) else "degraded",
            "Answer drafted from packed evidence.",
            {
                "cited_nodes": state["cited_nodes"],
                "structured": bool(structured),
                "degraded": state.get("degraded"),
            },
        )
        self._maybe_stop(state)
        return state

    # -- stage G: verify -------------------------------------------------
    def _stage_verify(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("verify", "started", "Resolving citations against persisted evidence.")
        packed_ids = list(state.get("packed_node_ids") or [])
        critic_result = self._critic.run(
            query=state["query"],
            context={
                "previous_output": {
                    "answer": state["answer"],
                    "cited_nodes": state["cited_nodes"],
                    "alerts": state["alerts"],
                },
                "packed_node_ids": packed_ids,
            },
            previous_results=[],
        )
        critic_data = critic_result.data or {}
        extra_alerts = self._citation_guard(
            state["cited_nodes"], packed_ids, self.ctxflow
        )
        alerts = list(critic_data.get("alerts") or []) + extra_alerts
        citations, source_alerts = self._resolve_citations(
            state["cited_nodes"], self.ctxflow
        )
        alerts += source_alerts
        state["citations"] = citations
        verdict = str(critic_data.get("verdict") or "INSUFFICIENT_EVIDENCE")
        if extra_alerts:
            if any(item.get("type") == "invalid_citation" for item in extra_alerts):
                verdict = "WARNINGS_FOUND"
            if not state["cited_nodes"]:
                verdict = "INSUFFICIENT_EVIDENCE"
        state["verdict"] = verdict
        state["validations"] = critic_data.get("validations") or []
        state["alerts"] = alerts

        # Repair: only when the verifier found citations that can actually be
        # excluded from the pack. This keeps the loop bounded and corrective
        # (a plain re-run with identical input would change nothing).
        repairs = int(state.get("repairs") or 0)
        max_repairs = int(state.get("max_repairs") or 0)
        flagged = {
            item.get("node_id")
            for item in alerts
            if item.get("type") in {"invalid_citation", "superseded"}
            and item.get("node_id")
        }
        already_excluded = set(state.get("excluded_node_ids") or [])
        new_exclusions = flagged - already_excluded
        needs_repair = bool(
            verdict == "WARNINGS_FOUND" and new_exclusions and repairs < max_repairs
        )
        if needs_repair:
            state["excluded_node_ids"] = sorted(already_excluded | new_exclusions)
            state["repairs"] = repairs + 1
        state["needs_repair"] = needs_repair

        self._emit(
            "verify",
            "completed",
            f"Verification verdict: {verdict}",
            {
                "verdict": verdict,
                "validations": state["validations"],
                "alerts": alerts,
                "repair": needs_repair,
                "repair_attempt": state.get("repairs", 0),
            },
        )
        self._maybe_stop(state)
        return state

    # -- stage H: persist ------------------------------------------------
    def _stage_persist(self, state: ResearchState) -> ResearchState:
        if state.get("stop"):
            return state
        self._emit("persist", "started", "Saving the research run.")
        if self.persist is not None:
            self.persist()
        self._emit("persist", "completed", "Run record ready.")
        return state

    # -- synthesis helpers ----------------------------------------------
    @staticmethod
    def _insufficient_answer(state: ResearchState) -> str:
        """Explain an empty evidence pack when external sources failed."""
        search = state.get("external_search") or {}
        failed_sources = [
            s for s in state.get("sources") or [] if s.get("access_status") == "failed"
        ]
        if search.get("status") not in {"error", "partial"} and not failed_sources:
            return ""
        reasons = [f"{k}: {v}" for k, v in (search.get("errors") or {}).items()]
        reasons += [
            f"{s.get('url')}: {s.get('metadata', {}).get('fetch_error', 'fetch failed')}"
            for s in failed_sources
        ]
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        return (
            "Insufficient evidence: project memory had nothing relevant and the "
            f"external sources could not supply usable evidence{detail}."
        )

    @staticmethod
    def _source_label(node: Any) -> str:
        meta = node.metadata or {}
        url = meta.get("source_url") or meta.get("url") or ""
        access = meta.get("access_status") or ""
        parts = [f"url={url}" if url else "", f"access={access}" if access else ""]
        label = " ".join(p for p in parts if p)
        return f" [source {label}]" if label else ""
    def _maybe_structured_answer(
        self,
        query: str,
        packed_ids: List[str],
        *,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        token_budget: int = 2048,
    ) -> Optional[Dict[str, Any]]:
        llm = self.llm
        complete_json = getattr(llm, "complete_json", None)
        if not callable(complete_json) or not packed_ids:
            return None
        evidence_blocks = []
        for node_id in packed_ids:
            node = self.ctxflow.graph.get_node(node_id)
            if node is None:
                continue
            evidence_blocks.append(
                wrap_untrusted(
                    f"[Node:{node.id}] ({node.node_type}){self._source_label(node)} "
                    f"{node.summary or node.content[:400]}",
                    source=f"node:{node.id}",
                )
            )
        conversation_context = format_conversation_history(
            conversation_history or [], token_budget
        )
        history_section = (
            f"\n\n{conversation_context}" if conversation_context else ""
        )
        prompt = (
            f"Question: {query}{history_section}\n\nEvidence:\n"
            + "\n\n".join(evidence_blocks)
            + "\n\nAllowed node IDs: "
            + ", ".join(packed_ids)
        )
        try:
            parsed = complete_json(prompt, system=_SYNTHESIS_JSON_SYSTEM)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Structured synthesis failed: %s", exc)
            return None
        if not isinstance(parsed, dict):
            return None
        citations = parsed.get("citation_ids") or []
        if isinstance(citations, list):
            parsed["citation_ids"] = [
                item for item in citations if isinstance(item, str) and item in packed_ids
            ]
        return parsed

    @staticmethod
    def _format_structured(payload: Dict[str, Any]) -> str:
        parts = [str(payload.get("direct_answer") or "").strip()]
        supporting = payload.get("supporting_evidence") or []
        if supporting:
            parts.append("Supporting evidence:")
            for item in supporting:
                if isinstance(item, dict):
                    parts.append(
                        f"- [{item.get('node_id')}] {item.get('note') or ''}".strip()
                    )
        contradictory = payload.get("contradictory_evidence") or []
        if contradictory:
            parts.append("Contradictory or limiting evidence:")
            for item in contradictory:
                if isinstance(item, dict):
                    parts.append(
                        f"- [{item.get('node_id')}] {item.get('note') or ''}".strip()
                    )
        uncertainties = payload.get("uncertainties") or []
        if uncertainties:
            parts.append("Uncertainties:")
            for item in uncertainties:
                parts.append(f"- {item}")
        follow_ups = payload.get("follow_ups") or []
        if follow_ups:
            parts.append("Suggested follow-ups:")
            for item in follow_ups:
                parts.append(f"- {item}")
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _resolve_citations(
        cited: List[str], ctxflow: CtxFlow
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Resolve cited nodes to their source URL and access status."""
        citations: List[Dict[str, Any]] = []
        alerts: List[Dict[str, Any]] = []
        for node_id in cited:
            node = ctxflow.graph.get_node(node_id)
            if node is None:
                continue
            meta = node.metadata or {}
            source_node = node if node.node_type == "paper" else None
            source_node_id = meta.get("source_paper_node_id")
            if source_node is None and source_node_id:
                source_node = ctxflow.graph.get_node(source_node_id)
            source_meta = (source_node or node).metadata or {}
            url = meta.get("source_url") or source_meta.get("url") or ""
            access = meta.get("access_status") or source_meta.get("access_status") or ""
            status = node.status if isinstance(node.status, str) else node.status.value
            citations.append(
                {
                    "node_id": node_id,
                    "node_type": node.node_type,
                    "status": status,
                    "summary": (node.summary or node.content or "")[:200],
                    "source_node_id": source_node.id if source_node is not None else "",
                    "title": source_meta.get("title") or meta.get("source_paper") or "",
                    "source_url": url,
                    "provider": meta.get("provider") or source_meta.get("provider") or "",
                    "source_kind": meta.get("source_kind") or source_meta.get("source_kind") or "",
                    "access_status": access,
                    "retrieved_at": meta.get("retrieved_at") or source_meta.get("retrieved_at") or "",
                }
            )
            if (meta.get("source_kind") or source_meta.get("source_kind")) and not (url and access):
                alerts.append(
                    {
                        "type": "missing_source",
                        "node_id": node_id,
                        "message": f"Cited node [{node_id}] lacks a source URL or access status.",
                    }
                )
        return citations, alerts

    @staticmethod
    def _citation_guard(
        cited: List[str], packed_ids: List[str], ctxflow: CtxFlow
    ) -> List[Dict[str, Any]]:
        alerts: List[Dict[str, Any]] = []
        packed = set(packed_ids)
        for node_id in cited:
            node = ctxflow.graph.get_node(node_id)
            if node is None:
                alerts.append(
                    {
                        "type": "invalid_citation",
                        "node_id": node_id,
                        "message": f"Cited node [{node_id}] does not exist in project memory.",
                    }
                )
                continue
            status = node.status if isinstance(node.status, str) else node.status.value
            if status == "superseded":
                alerts.append(
                    {
                        "type": "superseded",
                        "node_id": node_id,
                        "superseded_by": node.superseded_by,
                        "message": f"Cited node [{node_id}] is superseded and is not current evidence.",
                    }
                )
            if packed and node_id not in packed:
                alerts.append(
                    {
                        "type": "invalid_citation",
                        "node_id": node_id,
                        "message": (
                            f"Cited node [{node_id}] was not in the packed context."
                        ),
                    }
                )
        return alerts

    # -- shared utilities ------------------------------------------------
    def _timed_out(self, started: float) -> bool:
        return (time.monotonic() - started) > self.max_wall_s

    @staticmethod
    def _cancelled(
        cancel_event: Optional[threading.Event],
        state: ResearchState,
        emit: Callable[..., None],
    ) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            state["cancelled"] = True
            emit("persist", "cancelled", "Run cancelled.")
            return True
        return False

    @staticmethod
    def _status_from_state(state: ResearchState) -> tuple[bool, str]:
        """Map run state to ``(success, status)``.

        ``degraded`` is still a *successful* run: the graph produced an answer
        and a verdict, but a model call, a paper reader, an external provider,
        or the miss part of the pipeline was unavailable. Only a guardrail
        block, cancellation, or an exceeded wall-clock budget (all of which set
        ``stop``) is a failed run. The accumulated ``errors`` strings are kept
        for the UI; they do not by themselves fail the run.
        """
        if state.get("stop"):
            return False, str(state.get("stop_status") or "failed")
        if state.get("degraded"):
            return True, "degraded"
        return True, "completed"

    def _finalize(
        self,
        state: ResearchState,
        events: List[Dict[str, Any]],
        *,
        success: bool,
        status: str,
    ) -> Dict[str, Any]:
        return {
            "run_id": state.get("run_id"),
            "project_id": state.get("project_id"),
            "session_id": state.get("session_id") or "default",
            "query": state.get("query"),
            "status": status,
            "success": success,
            "engine": state.get("engine") or self.engine,
            "intent": state.get("intent"),
            "plan": state.get("plan"),
            "research_plan": state.get("research_plan") or {},
            "tasks": state.get("tasks") or [],
            "sources": state.get("sources") or [],
            "citations": state.get("citations") or [],
            "stage": events[-1]["stage"] if events else "plan",
            "answer": state.get("answer") or "",
            "verdict": state.get("verdict") or "",
            "cited_nodes": state.get("cited_nodes") or [],
            "packed_node_ids": state.get("packed_node_ids") or [],
            "alerts": state.get("alerts") or [],
            "validations": state.get("validations") or [],
            "context_usage": state.get("context_usage") or {},
            "external_search": state.get("external_search"),
            "ingested_papers": state.get("ingested_papers") or [],
            "structured": state.get("structured") or {},
            "errors": state.get("errors") or [],
            "degraded": bool(state.get("degraded")),
            "repairs": int(state.get("repairs") or 0),
            "excluded_node_ids": state.get("excluded_node_ids") or [],
            "events": events,
            "graph_nodes": self.ctxflow.graph.node_count,
            "graph_edges": self.ctxflow.graph.edge_count,
        }


class ResearchGraph(ResearchWorkflow):
    """LangGraph ``StateGraph`` engine over the shared eight stages.

    The graph adds three things the sequential loop cannot express directly:

    * an explicit node/edge topology (inspectable and checkpointed),
    * a ``verify -> pack`` conditional edge for bounded repair, and
    * a checkpointer keyed by ``run_id`` (``thread_id``), so the final state of
      any run can be inspected after the fact.

    It returns exactly the same result dictionary and emits exactly the same
    events as :class:`ResearchWorkflow`.
    """

    engine = "langgraph"

    def __init__(
        self,
        ctxflow: CtxFlow,
        *,
        literature_search: Optional[LiteratureSearchSkill] = None,
        paper_reader: Optional[PaperReaderSkill] = None,
        llm: Any = None,
        persist: Optional[Callable[[], None]] = None,
        max_wall_s: float = 90.0,
        max_tool_calls: int = 12,
        max_repairs: int = 1,
        checkpointer: Any = None,
        web_search: Optional[WebSearchSkill] = None,
        source_fetch: Optional[SourceFetchSkill] = None,
        limits: Optional[ResearchLimits] = None,
    ) -> None:
        super().__init__(
            ctxflow,
            literature_search=literature_search,
            paper_reader=paper_reader,
            llm=llm,
            persist=persist,
            max_wall_s=max_wall_s,
            max_tool_calls=max_tool_calls,
            max_repairs=max_repairs,
            web_search=web_search,
            source_fetch=source_fetch,
            limits=limits,
        )
        self._checkpointer = checkpointer
        self._compiled: Any = None

    # -- graph construction ---------------------------------------------
    def _make_checkpointer(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer
        try:
            from langgraph.checkpoint.memory import MemorySaver

            self._checkpointer = MemorySaver()
        except ImportError:  # pragma: no cover - older/newer layouts
            from langgraph.checkpoint.memory import InMemorySaver

            self._checkpointer = InMemorySaver()
        return self._checkpointer

    def _build(self) -> Any:
        if self._compiled is not None:
            return self._compiled

        from langgraph.graph import END, START, StateGraph

        graph: Any = StateGraph(ResearchState)
        graph.add_node("plan", self._node(self._stage_plan))
        graph.add_node("retrieve", self._node(self._stage_retrieve))
        graph.add_node("read", self._node(self._stage_read))
        graph.add_node("memory", self._node(self._stage_memory))
        graph.add_node("pack", self._node(self._stage_pack))
        graph.add_node("synthesize", self._node(self._stage_synthesize))
        graph.add_node("verify", self._node(self._stage_verify))
        graph.add_node("persist", self._node(self._stage_persist))

        graph.add_edge(START, "plan")
        graph.add_conditional_edges(
            "plan", self._route_after_plan, {"retrieve": "retrieve", END: END}
        )
        graph.add_conditional_edges(
            "retrieve", self._route_after_retrieve, {"read": "read", END: END}
        )
        graph.add_conditional_edges(
            "read", self._route_after_read, {"memory": "memory", END: END}
        )
        graph.add_conditional_edges(
            "memory", self._route_after_memory, {"pack": "pack", END: END}
        )
        graph.add_conditional_edges(
            "pack", self._route_after_pack, {"synthesize": "synthesize", END: END}
        )
        graph.add_conditional_edges(
            "synthesize",
            self._route_after_synthesize,
            {"verify": "verify", END: END},
        )
        graph.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {"pack": "pack", "persist": "persist", END: END},
        )
        graph.add_edge("persist", END)

        self._compiled = graph.compile(checkpointer=self._make_checkpointer())
        return self._compiled

    def _node(self, stage: Callable[[ResearchState], ResearchState]) -> Callable[[ResearchState], ResearchState]:
        def run_stage(state: ResearchState) -> ResearchState:
            return stage(state)

        return run_stage

    @staticmethod
    def _stopped(state: ResearchState) -> bool:
        return bool(state.get("stop"))

    @staticmethod
    def _end() -> str:
        # Imported lazily so importing this module never requires LangGraph.
        from langgraph.graph import END

        return END

    def _route_after_plan(self, state: ResearchState) -> str:
        return self._end() if self._stopped(state) else "retrieve"

    def _route_after_retrieve(self, state: ResearchState) -> str:
        return self._end() if self._stopped(state) else "read"

    def _route_after_read(self, state: ResearchState) -> str:
        return self._end() if self._stopped(state) else "memory"

    def _route_after_memory(self, state: ResearchState) -> str:
        return self._end() if self._stopped(state) else "pack"

    def _route_after_pack(self, state: ResearchState) -> str:
        return self._end() if self._stopped(state) else "synthesize"

    def _route_after_synthesize(self, state: ResearchState) -> str:
        return self._end() if self._stopped(state) else "verify"

    def _route_after_verify(self, state: ResearchState) -> str:
        if self._stopped(state):
            return self._end()
        if state.get("needs_repair"):
            return "pack"
        return "persist"

    # -- execution -------------------------------------------------------
    def run(
        self,
        query: str,
        *,
        run_id: str,
        project_id: str = "default",
        session_id: str = "default",
        conversation_history: Optional[List[Dict[str, str]]] = None,
        token_budget: int = 2048,
        on_event: Optional[EventCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        self._begin_run(run_id, on_event, cancel_event)
        state = self._initial_state(
            query,
            run_id,
            project_id,
            session_id,
            conversation_history or [],
            token_budget,
        )
        config = {
            "configurable": {"thread_id": run_id},
            "recursion_limit": 50,
        }

        try:
            compiled = self._build()
            final_state = compiled.invoke(state, config=config)
        except Exception as exc:  # noqa: BLE001 — never report a crash as success
            logger.exception("LangGraph research run failed")
            recovered: ResearchState = dict(state)
            try:
                snapshot = compiled.get_state(config)
                if snapshot and snapshot.values:
                    recovered = dict(snapshot.values)
            except Exception:  # noqa: BLE001 - best effort recovery
                pass
            recovered["engine"] = self.engine
            recovered["errors"] = list(recovered.get("errors") or []) + [str(exc)]
            recovered["degraded"] = True
            self._emit("persist", "failed", str(exc))
            return self._finalize(
                recovered, self._events, success=False, status="failed"
            )

        success, status = self._status_from_state(final_state)
        return self._finalize(final_state, self._events, success=success, status=status)

    def get_state(self, run_id: str) -> Any:
        """Return the checkpoint snapshot for ``run_id`` (None before a run)."""
        compiled = self._build()
        try:
            return compiled.get_state({"configurable": {"thread_id": run_id}})
        except Exception:  # noqa: BLE001
            return None


def maybe_langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
    except ImportError:
        return False
    return True


def build_orchestrator(
    ctxflow: CtxFlow,
    *,
    literature_search: Optional[LiteratureSearchSkill] = None,
    paper_reader: Optional[PaperReaderSkill] = None,
    llm: Any = None,
    persist: Optional[Callable[[], None]] = None,
    max_wall_s: float = 90.0,
    max_tool_calls: int = 12,
    max_repairs: int = 1,
    checkpointer: Any = None,
    web_search: Optional[WebSearchSkill] = None,
    source_fetch: Optional[SourceFetchSkill] = None,
    limits: Optional[ResearchLimits] = None,
) -> ResearchWorkflow:
    """Return the preferred orchestrator for this process.

    ``RESEARCHOS_ORCHESTRATOR=sequential`` forces the plain loop. Otherwise
    LangGraph is used when importable, falling back to the loop when it is not.
    Tests and embedders can also pass an explicit checkpointer.
    """
    kwargs = {
        "literature_search": literature_search,
        "paper_reader": paper_reader,
        "llm": llm,
        "persist": persist,
        "max_wall_s": max_wall_s,
        "max_tool_calls": max_tool_calls,
        "max_repairs": max_repairs,
        "web_search": web_search,
        "source_fetch": source_fetch,
        "limits": limits,
    }
    if _orchestrator_preference() == "langgraph" and maybe_langgraph_available():
        return ResearchGraph(ctxflow, checkpointer=checkpointer, **kwargs)
    return ResearchWorkflow(ctxflow, **kwargs)
