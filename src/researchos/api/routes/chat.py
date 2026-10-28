"""Chat Routes — chat, query, and search endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from researchos.api.graph_revision import bump_graph_revision
from researchos.api.schemas import (
    ChatRequest,
    ChatResponse,
    ProviderOutcomeResponse,
    QueryRequest,
    QueryResponse,
    QueryResultResponse,
    SearchRequest,
    SearchResponse,
    SearchResultResponse,
)
from researchos.persistence import new_run_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


# ---------------------------------------------------------------------------
# These will be populated at app startup via dependency injection
# ---------------------------------------------------------------------------
_app_state: dict = {}


def set_app_state(state: dict) -> None:
    """Inject application state (CtxFlow, router agent, etc.)."""
    global _app_state
    _app_state = state


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Process a natural-language research query.

    Routes the query through the agent pipeline and returns a
    synthesized answer with citations and alerts.
    """
    skill_router = _app_state.get("router")
    if skill_router is None:
        raise HTTPException(503, detail="Agent router not initialized")

    workspace = _app_state.get("workspace")
    run_id = new_run_id()
    session_id = request.session_id or "default"
    started_at = time.time()
    conversation_history = (
        workspace.conversation_history(request.project_id, session_id)
        if workspace is not None
        else []
    )

    try:
        if workspace is not None:
            workspace.save_run(
                {
                    "run_id": run_id,
                    "project_id": request.project_id,
                    "session_id": session_id,
                    "query": request.query,
                    "status": "running",
                    "success": None,
                    "started_at": started_at,
                    "ended_at": None,
                    "answer": "",
                }
            )
        result = skill_router.run(
            query=request.query,
            context={
                "token_budget": request.token_budget,
                "conversation_history": conversation_history,
            },
        )
        bump_graph_revision(request.project_id)
        if workspace is not None:
            workspace.save_graph(request.project_id)

        data = result.data or {}
        pipeline_result = data.get("result", {}) or {}

        answer = ""
        cited_nodes: list = []
        alerts: list = []

        if isinstance(pipeline_result, dict):
            answer = pipeline_result.get("answer", "")
            cited_nodes = pipeline_result.get("cited_nodes", [])
            alerts = pipeline_result.get("alerts", [])

        # External search outcome (set by the router from the retriever step).
        external_search = data.get("external_search")

        # Build trace if requested
        trace = None
        if request.include_trace and cited_nodes:
            ctxflow = _app_state.get("ctxflow")
            if ctxflow:
                first_node = cited_nodes[0]
                trace_obj = ctxflow.research_trace(first_node)
                trace = {
                    "answer_node_id": trace_obj.answer_node_id,
                    "chain_length": len(trace_obj.supporting_chain.steps),
                    "superseded_count": len(trace_obj.superseded_claims),
                    "contradiction_count": len(trace_obj.contradicting_claims),
                    "source_paper_count": len(trace_obj.source_papers),
                }

        if workspace is not None:
            workspace.save_run(
                {
                    "run_id": run_id,
                    "project_id": request.project_id,
                    "session_id": session_id,
                    "query": request.query,
                    "status": "completed" if result.success else "failed",
                    "success": result.success,
                    "started_at": started_at,
                    "ended_at": time.time(),
                    "answer": answer,
                    "intent": data.get("intent", ""),
                    "cited_nodes": cited_nodes,
                    "alerts": alerts,
                    "context_usage": result.metadata.get("context_usage", {}),
                    "external_search": external_search,
                    "errors": [
                        item.get("error", "")
                        for item in data.get("pipeline_results", [])
                        if not item.get("success")
                    ],
                }
            )

        return ChatResponse(
            run_id=run_id,
            session_id=session_id,
            answer=answer,
            intent=data.get("intent", ""),
            cited_nodes=cited_nodes,
            alerts=alerts,
            trace=trace,
            metadata=result.metadata,
            external_search=external_search,
        )
    except Exception as exc:
        if workspace is not None:
            try:
                workspace.save_run(
                    {
                        "run_id": run_id,
                        "project_id": request.project_id,
                        "session_id": session_id,
                        "query": request.query,
                        "status": "failed",
                        "success": False,
                        "started_at": started_at,
                        "ended_at": time.time(),
                        "answer": "",
                        "errors": [str(exc)],
                    }
                )
            except Exception:
                logger.exception("Could not persist failed chat request")
        logger.error("Chat endpoint failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

@router.post("/query", response_model=QueryResponse)
async def query_graph(request: QueryRequest) -> QueryResponse:
    """Query the knowledge graph directly."""
    ctxflow = _app_state.get("ctxflow")
    if ctxflow is None:
        raise HTTPException(503, detail="CtxFlow not initialized")

    try:
        results = ctxflow.query(
            context=request.query,
            k=request.k,
            anchor=request.anchor,
            include_superseded=request.include_superseded,
        )

        response_results = []
        for r in results:
            response_results.append(QueryResultResponse(
                node_id=r.node.id,
                content=r.node.content,
                summary=r.node.summary,
                score=r.score,
                node_type=r.node.node_type,
                status=r.node.status if isinstance(r.node.status, str) else r.node.status.value,
                tags=sorted(r.node.tags),
                score_breakdown=r.score_breakdown,
            ))

        return QueryResponse(results=response_results, total=len(results))
    except Exception as exc:
        logger.error("Query endpoint failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# Literature Search
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchResponse)
async def search_literature(request: SearchRequest) -> SearchResponse:
    """Search external literature databases.

    The response always carries a ``status``: a provider outage reports
    ``error`` (or ``partial``) with per-provider errors instead of looking like
    a successful empty search.
    """
    search_skill = _app_state.get("literature_search")
    if search_skill is None:
        raise HTTPException(503, detail="Literature search not initialized")

    try:
        search_with_outcome = getattr(search_skill, "search_with_outcome", None)
        if callable(search_with_outcome):
            outcome = search_with_outcome(
                query=request.query,
                max_results=request.max_results,
                provider=request.provider,
            )
            results = list(outcome.results)
            status = outcome.status.value
            provider_name = outcome.provider
            providers = {
                item.provider: ProviderOutcomeResponse(**item.to_dict())
                for item in outcome.outcomes
            }
            errors = dict(outcome.errors)
            elapsed_ms = round(outcome.elapsed_ms, 3)
        else:
            # Injected/custom skills that only implement search() cannot report
            # a provider status; say so instead of implying success.
            results = list(
                search_skill.search(
                    query=request.query,
                    max_results=request.max_results,
                    provider=request.provider,
                )
            )
            status = "unknown"
            provider_name = request.provider
            providers = {}
            errors = {}
            elapsed_ms = 0.0

        response_results = [
            SearchResultResponse(
                title=r.title,
                authors=r.authors,
                abstract=r.abstract,
                year=r.year,
                doi=r.doi,
                arxiv_id=r.arxiv_id,
                url=r.url,
                source=r.source,
            )
            for r in results
        ]

        return SearchResponse(
            results=response_results,
            total=len(results),
            status=status,
            provider=provider_name,
            providers=providers,
            errors=errors,
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        logger.error("Search endpoint failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))
