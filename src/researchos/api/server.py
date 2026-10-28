"""FastAPI Application Server for ResearchOS.

Creates and configures the FastAPI application instance:
- CORS middleware
- Lifespan context initializing CtxFlow, ResearchWorkspace, Skills, Agents, Workflow
- SSE (Server-Sent Events) streaming endpoint for live 8-stage workflow events
- REST routes (chat, papers, notes, projects, runs)
- Health checks and skill registry discovery
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

# ── Load .env before anything reads os.environ ────────────────────────────────
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=Path(__file__).parents[3] / ".env", override=False)
except ImportError:
    pass  # python-dotenv optional — set env vars manually if not installed
# ──────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from ctxflow import CtxFlow
from ctxflow.models import CtxFlowConfig
from researchos.agents.critic import CriticAgent
from researchos.agents.reader import ReaderAgent
from researchos.agents.retriever import RetrieverAgent
from researchos.agents.router import SkillRouter
from researchos.agents.synthesizer import SynthesizerAgent
from researchos.agents.trace import TraceAgent
from researchos.api.routes import chat as chat_routes
from researchos.api.routes import notes as notes_routes
from researchos.api.routes import papers as papers_routes
from researchos.api.routes import projects as projects_routes
from researchos.api.routes import runs as runs_routes
from researchos.api.schemas import (
    HealthResponse,
    ModelUsageResponse,
    RunResponse,
    SkillInfoResponse,
    SkillListResponse,
)
from researchos.gateway import ModelGateway, create_model_gateway
from researchos.orchestration.research_tasks import ResearchLimits
from researchos.orchestration.workflow import build_orchestrator
from researchos.persistence import ResearchWorkspace, new_run_id
from researchos.pipeline import make_router, redact_secrets
from researchos.skills.graph_query import GraphQuerySkill
from researchos.skills.literature_search import LiteratureSearchSkill
from researchos.skills.paper_reader import PaperReaderSkill
from researchos.skills.registry import SkillRegistry
from researchos.skills.review_writer import ReviewWriterSkill
from researchos.skills.source_fetch import SourceFetchSkill
from researchos.skills.web_search import WebSearchSkill
from researchos.memory.context import create_context_provider

logger = logging.getLogger(__name__)

# Bundled static workspace UI (works from a source checkout and a wheel).
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(
    ctxflow: Optional[CtxFlow] = None,
    skill_registry: Optional[SkillRegistry] = None,
    router_agent: Optional[SkillRouter] = None,
    memory_store: Any | None = None,
    enable_cors: bool = True,
    model_gateway: Optional[ModelGateway] = None,
    workspace: Optional[ResearchWorkspace] = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Parameters
    ----------
    ctxflow : CtxFlow | None
        Existing CtxFlow instance. When *workspace* is also None, this is
        the single shared graph (legacy / test mode). When *workspace* is
        provided, this is ignored for project-scoped runs and only used as the
        default fallback for the old /api/chat route.
    skill_registry : SkillRegistry | None
        Existing SkillRegistry instance. If None, created and populated.
    router_agent : SkillRouter | None
        Existing SkillRouter instance. If None, composed with all agents.
    enable_cors : bool
        Whether to enable permissive CORS for web frontends.
    model_gateway : ModelGateway | None
        Injected gateway (tests/embedders). If None, the gateway is created
        from ``RESEARCHOS_MODEL_*`` only when ``RESEARCHOS_MODEL_ENABLED`` is
        truthy; otherwise no model is wired.
    workspace : ResearchWorkspace | None
        Injected workspace (tests). If None, created from env / data dir.
    """

    # Shared state container
    state: Dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ctxflow, skill_registry, router_agent, workspace

        # 1a. Resolve the ResearchWorkspace (project-scoped persistence)
        if workspace is None:
            workspace = ResearchWorkspace(injected_ctxflow=ctxflow)
        state["workspace"] = workspace

        # 1b. Resolve the default CtxFlow (used for legacy /api/chat)
        if ctxflow is None:
            ctxflow = workspace.get_graph("default")
        state["ctxflow"] = ctxflow
        if memory_store is not None:
            provider = create_context_provider(memory_store)
            if provider is not None:
                state["context_provider"] = provider
                app.state.context_provider = provider

        # 1c. Resolve the optional model gateway — gracefully degrade when
        #     the API key is missing or the provider is misconfigured.
        from researchos.gateway.model import ModelConfigError  # local import to avoid circular
        owns_gateway = model_gateway is None
        gateway = model_gateway
        if gateway is None:
            try:
                gateway = create_model_gateway()
            except (ModelConfigError, Exception) as exc:
                logger.warning(
                    "Model gateway not available (%s: %s); running in heuristic mode.",
                    type(exc).__name__, exc,
                )
                gateway = None
        state["model_gateway"] = gateway
        llm = gateway if gateway is not None else None
        if gateway is not None:
            logger.info(
                "Model gateway enabled: provider=%s model=%s",
                gateway.config.provider,
                gateway.config.model_id,
            )

        # 2. Initialize Skills
        #    arXiv search goes through the MCP server (``uvx arxiv-mcp-server``)
        #    when available; LiteratureSearchSkill falls back to the HTTP API
        #    on any MCP failure so the demo still works offline.
        arxiv_mcp = None
        try:
            from researchos.mcp import ArxivMCPClient
            from researchos.mcp.arxiv import mcp_enabled

            if mcp_enabled():
                arxiv_mcp = ArxivMCPClient()
                logger.info("arXiv MCP client enabled (uvx arxiv-mcp-server)")
        except Exception as exc:  # noqa: BLE001 — MCP is optional
            logger.warning("arXiv MCP client unavailable: %s", exc)
            arxiv_mcp = None
        state["arxiv_mcp"] = arxiv_mcp

        lit_search = LiteratureSearchSkill(arxiv_client=arxiv_mcp)
        research_limits = ResearchLimits.from_env()
        web_search = WebSearchSkill(max_results=research_limits.results_per_provider)
        source_fetch = SourceFetchSkill(
            timeout=research_limits.fetch_timeout_s,
            max_bytes=research_limits.max_page_bytes,
        )
        paper_reader = PaperReaderSkill(llm=llm)
        graph_query = GraphQuerySkill(ctxflow)
        review_writer = ReviewWriterSkill(ctxflow, llm=llm)

        state["literature_search"] = lit_search
        state["web_search"] = web_search
        state["source_fetch"] = source_fetch
        state["research_limits"] = research_limits
        state["paper_reader"] = paper_reader
        state["graph_query"] = graph_query
        state["review_writer"] = review_writer

        # 3. Populate SkillRegistry
        if skill_registry is None:
            skill_registry = SkillRegistry()

            skill_registry.register_function(
                name="web_search",
                fn=lambda query, max_results=None: web_search.search_with_outcome(
                    query, max_results
                ).to_dict(),
                description=(
                    "Search the general web (opt-in: RESEARCHOS_WEB_SEARCH=on). "
                    "Returns an outcome summary, never page content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": ["integer", "null"], "minimum": 1},
                    },
                    "required": ["query"],
                },
                output_schema={"type": "object"},
                tags=["search", "web"],
            )
            skill_registry.register_function(
                name="fetch_source",
                fn=lambda url: source_fetch.fetch(url).to_dict(),
                description=(
                    "Fetch one public HTTP(S) page with size, redirect, robots and "
                    "private-network limits. Returns a fetch summary."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                output_schema={"type": "object"},
                tags=["fetch", "web", "source"],
            )
            skill_registry.register_function(
                name="literature_search",
                fn=lit_search.search,
                description="Search academic papers on arXiv and Semantic Scholar.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1},
                        "provider": {
                            "type": "string",
                            "enum": [
                                "arxiv",
                                "semantic_scholar",
                                "openalex",
                                "both",
                            ],
                        },
                    },
                    "required": ["query"],
                },
                output_schema={"type": "array"},
                tags=["search", "literature", "arxiv"],
            )
            skill_registry.register_function(
                name="paper_reader",
                fn=paper_reader.read_pdf,
                description="Extract structured sections and text from PDF documents.",
                input_schema={
                    "type": "object",
                    "properties": {"pdf_path": {"type": "string"}},
                    "required": ["pdf_path"],
                },
                output_schema={"type": "object"},
                tags=["pdf", "parsing", "reader"],
            )
            skill_registry.register_function(
                name="graph_query",
                fn=graph_query.query,
                description="Search internal CtxFlow knowledge graph with multi-signal ranking.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query_text": {"type": "string"},
                        "k": {"type": "integer", "minimum": 1},
                        "anchor": {"type": ["string", "null"]},
                        "include_superseded": {"type": "boolean"},
                        "token_budget": {"type": ["integer", "null"]},
                    },
                    "required": ["query_text"],
                },
                output_schema={"type": "array"},
                tags=["graph", "query", "memory"],
            )
            skill_registry.register_function(
                name="research_trace",
                fn=graph_query.research_trace,
                description="Trace provenance, supersession, and contradictions for a claim node.",
                input_schema={
                    "type": "object",
                    "properties": {"node_id": {"type": "string"}},
                    "required": ["node_id"],
                },
                output_schema={"type": "object"},
                tags=["trace", "provenance", "audit"],
            )
            skill_registry.register_function(
                name="review_writer",
                fn=review_writer.write_review,
                description="Synthesize structured multi-paper literature reviews.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "max_sources": {"type": "integer", "minimum": 1},
                        "include_superseded": {"type": "boolean"},
                        "token_budget": {"type": "integer", "minimum": 1},
                    },
                    "required": ["topic"],
                },
                output_schema={"type": "object"},
                tags=["synthesis", "review", "writing"],
            )
        state["skill_registry"] = skill_registry

        # 4. Initialize Multi-Agent Pipeline (legacy /api/chat path)
        if router_agent is None:
            retriever = RetrieverAgent(
                ctxflow=ctxflow, literature_search=lit_search, llm=llm
            )
            reader = ReaderAgent(
                ctxflow=ctxflow, paper_reader=paper_reader, llm=llm
            )
            synthesizer = SynthesizerAgent(ctxflow=ctxflow, llm=llm)
            critic = CriticAgent(ctxflow=ctxflow, llm=llm)
            tracer = TraceAgent(ctxflow=ctxflow, llm=llm)

            router_agent = SkillRouter(
                agents={
                    "retriever": retriever,
                    "reader": reader,
                    "synthesizer": synthesizer,
                    "critic": critic,
                    "graph_query": tracer,
                },
                llm=llm,
            )
        state["router"] = router_agent

        # Expose the wired objects on app.state for tests and embedders.
        app.state.ctxflow = ctxflow
        app.state.skill_registry = skill_registry
        app.state.router = router_agent
        app.state.model_gateway = gateway
        app.state.workspace = workspace

        # 5. Inject dependencies into routes
        chat_routes.set_app_state(state)
        papers_routes.set_app_state(state)
        notes_routes.set_app_state(state)
        projects_routes.set_app_state(state)
        runs_routes.set_app_state(state)

        yield

        # Teardown
        try:
            lit_search.close()
        except Exception:
            pass
        try:
            if state.get("arxiv_mcp") is not None:
                state["arxiv_mcp"].close()
        except Exception:
            pass
        if owns_gateway and gateway is not None:
            gateway.close()

    app = FastAPI(
        title="ResearchOS API",
        description="Autonomous Multi-Agent Scientific Research Operating System powered by CtxFlow",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.context_provider = None

    if enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Mount Route Modules
    app.include_router(chat_routes.router)
    app.include_router(papers_routes.router)
    app.include_router(notes_routes.router)
    app.include_router(projects_routes.router)
    app.include_router(runs_routes.router)

    # Root & Health Endpoints
    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        c = state.get("ctxflow")
        ws: Optional[ResearchWorkspace] = state.get("workspace")
        gw: Optional[ModelGateway] = state.get("model_gateway")
        nodes = c.graph.node_count if c else 0
        edges = c.graph.edge_count if c else 0
        return HealthResponse(
            status="ok",
            version="0.2.0",
            graph_nodes=nodes,
            graph_edges=edges,
            persistence=ws.backend if ws else "memory",
            model_enabled=gw is not None,
            model_provider=gw.config.provider if gw else None,
            model_id=gw.config.model_id if gw else None,
        )

    @app.get("/api/skills", response_model=SkillListResponse, tags=["skills"])
    async def list_skills() -> SkillListResponse:
        reg: Optional[SkillRegistry] = state.get("skill_registry")
        if not reg:
            return SkillListResponse(skills=[])
        schemas = reg.list_skills()
        return SkillListResponse(
            skills=[
                SkillInfoResponse(
                    name=s.name,
                    description=s.description,
                    version=s.version,
                    tags=s.tags,
                    input_schema=s.input_schema,
                    output_schema=s.output_schema,
                )
                for s in schemas
            ]
        )

    @app.get(
        "/api/model/usage",
        response_model=ModelUsageResponse,
        tags=["system"],
    )
    async def model_usage() -> ModelUsageResponse:
        """Cumulative model token/latency accounting for this process."""
        gateway: Optional[ModelGateway] = state.get("model_gateway")
        if gateway is None:
            return ModelUsageResponse(enabled=False)
        return ModelUsageResponse(
            enabled=True,
            provider=gateway.config.provider,
            model=gateway.config.model_id,
            usage=gateway.usage(),
            by_model=gateway.usage_by_model(),
        )

    # ---------------------------------------------------------------------------
    # Legacy SSE stream (old SkillRouter path — kept for backward compatibility)
    # ---------------------------------------------------------------------------

    @app.get("/api/stream", tags=["stream"])
    async def stream_query(
        query: str,
        request: Request,
        token_budget: int = 2048,
        project_id: str = "default",
        session_id: str = Query(
            "default",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._-]+$",
        ),
    ):
        """SSE streaming endpoint — 8-stage research execution.

        Runs on the LangGraph orchestrator by default (unless
        ``RESEARCHOS_ORCHESTRATOR=sequential`` or LangGraph is unavailable).
        Each event carries: event=stage_name, data=JSON with
        {run_id, stage, status, message, ts, data}.
        The final event is event=done with the full run record.
        """
        ws: Optional[ResearchWorkspace] = state.get("workspace")
        gw: Optional[ModelGateway] = state.get("model_gateway")
        lit_search: Optional[LiteratureSearchSkill] = state.get("literature_search")
        paper_reader_skill: Optional[PaperReaderSkill] = state.get("paper_reader")
        web_search_skill: Optional[WebSearchSkill] = state.get("web_search")
        source_fetch_skill: Optional[SourceFetchSkill] = state.get("source_fetch")
        limits: Optional[ResearchLimits] = state.get("research_limits")

        if not ws:
            raise HTTPException(503, detail="Workspace not initialized")

        run_id = new_run_id()
        started_at = time.time()
        conversation_history = ws.conversation_history(project_id, session_id)
        try:
            ws.save_run(
                {
                    "run_id": run_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "query": query,
                    "status": "running",
                    "success": None,
                    "started_at": started_at,
                    "ended_at": None,
                    "answer": "",
                }
            )
        except Exception as exc:
            logger.exception("Failed to persist incoming research prompt")
            raise HTTPException(
                500, detail="Could not persist the conversation prompt."
            ) from exc
        project_graph = ws.get_graph(project_id)

        def _save_graph() -> None:
            ws.save_graph(project_id)

        workflow = build_orchestrator(
            project_graph,
            literature_search=lit_search,
            paper_reader=paper_reader_skill,
            llm=gw,
            persist=_save_graph,
            web_search=web_search_skill,
            source_fetch=source_fetch_skill,
            limits=limits,
        )

        # Thread-safe event queue: workflow runs in a thread; SSE reads here
        event_queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(ev: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, ev)

        cancel_event = threading.Event()
        result_holder: Dict[str, Any] = {}

        def _run_workflow() -> None:
            try:
                result = workflow.run(
                    query=query,
                    run_id=run_id,
                    project_id=project_id,
                    session_id=session_id,
                    conversation_history=conversation_history,
                    token_budget=max(1, int(token_budget)),
                    on_event=on_event,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                logger.exception("Research workflow raised outside its failure handler")
                result = {
                    "run_id": run_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "query": query,
                    "status": "failed",
                    "success": False,
                    "answer": "",
                    "errors": [str(exc)],
                    "alerts": [],
                    "events": [],
                }
            result["session_id"] = session_id
            result["started_at"] = started_at
            result["ended_at"] = time.time()
            try:
                ws.save_run(result)
            except Exception:
                logger.exception("Failed to persist completed research run")
                result["degraded"] = True
                result["status"] = "degraded" if result.get("success") else "failed"
                result["errors"] = list(result.get("errors") or []) + [
                    "Conversation history could not be updated with the final answer."
                ]
                result["alerts"] = list(result.get("alerts") or []) + [
                    {
                        "type": "persistence",
                        "message": "The answer was generated but could not be saved to conversation history.",
                    }
                ]
            result_holder["result"] = result
            loop.call_soon_threadsafe(event_queue.put_nowait, None)  # sentinel

        async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
            # Start the workflow in a background thread
            thread = threading.Thread(target=_run_workflow, daemon=True)
            thread.start()

            yield {
                "event": "start",
                "data": json.dumps({
                    "run_id": run_id,
                    "query": query,
                    "project_id": project_id,
                    "session_id": session_id,
                    "engine": getattr(workflow, "engine", "sequential"),
                    "message": "Workflow started",
                }),
            }

            try:
                while True:
                    # Check for client disconnect
                    if await request.is_disconnected():
                        cancel_event.set()
                        break

                    try:
                        ev = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    if ev is None:  # sentinel — workflow finished
                        break

                    yield {
                        "event": "task" if ev.get("event") == "task" else ev.get("stage", "stage"),
                        "data": json.dumps(redact_secrets(ev)),
                    }
            finally:
                cancel_event.set()

            # Emit final done event
            result = result_holder.get("result") or {}

            yield {
                "event": "done",
                "data": json.dumps(redact_secrets({
                    "run_id": run_id,
                    "session_id": session_id,
                    "status": result.get("status", "unknown"),
                    "success": result.get("success", False),
                    "engine": result.get("engine", getattr(workflow, "engine", "sequential")),
                    "answer": result.get("answer", ""),
                    "verdict": result.get("verdict", ""),
                    "cited_nodes": result.get("cited_nodes", []),
                    "alerts": result.get("alerts", []),
                    "context_usage": result.get("context_usage", {}),
                    "external_search": result.get("external_search"),
                    "degraded": result.get("degraded", False),
                    "errors": result.get("errors", []),
                    "graph_nodes": result.get("graph_nodes", 0),
                    "graph_edges": result.get("graph_edges", 0),
                    "structured": result.get("structured", {}),
                    "research_plan": result.get("research_plan", {}),
                    "tasks": result.get("tasks", []),
                    "sources": result.get("sources", []),
                    "citations": result.get("citations", []),
                })),
            }

        return EventSourceResponse(event_generator())

    # Workspace UI: mounted last so /api, /docs and /openapi.json win.
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="workspace")
    else:  # pragma: no cover - only if a wheel was built without package data
        logger.warning("Workspace UI assets not found at %s", WEB_DIR)

    return app
