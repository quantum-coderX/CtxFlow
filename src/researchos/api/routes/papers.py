"""Paper & Graph Routes — paper ingestion, graph data, and trace endpoints."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ctxflow.models import NodeStatus
from researchos.api.schemas import (
    DemoSeedRequest,
    DemoSeedResponse,
    EdgeResponse,
    GraphDataResponse,
    GraphStatsResponse,
    NodeResponse,
    PaperIngestRequest,
    PaperIngestResponse,
    TraceRequest,
    TraceResponse,
    TraceStepResponse,
)
from researchos.api.graph_revision import bump_graph_revision, get_graph_revision
from researchos.demo import seed_demo_corpus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["papers", "graph"])


# ---------------------------------------------------------------------------
# Shared app state (injected at startup)
# ---------------------------------------------------------------------------
_app_state: dict = {}


def set_app_state(state: dict) -> None:
    """Inject application state."""
    global _app_state
    _app_state = state


# ---------------------------------------------------------------------------
# Paper Ingestion
# ---------------------------------------------------------------------------

def _superseded_count(ctxflow: Any) -> int:
    return sum(
        1
        for node in ctxflow.graph.all_nodes()
        if node.status == NodeStatus.SUPERSEDED
    )


def _title_tags(title: str) -> set:
    tags = set()
    for word in title.lower().split():
        cleaned = word.strip(".,;:!?()[]{}\"'")
        if len(cleaned) > 3:
            tags.add(cleaned)
    return tags


def _resolve_ctxflow(project_id: str = "default") -> Any:
    """Return the project-scoped CtxFlow graph, falling back to the global."""
    ws = _app_state.get("workspace")
    if ws is not None:
        return ws.get_graph(project_id)
    ctx = _app_state.get("ctxflow")
    if ctx is None:
        raise HTTPException(503, detail="CtxFlow not initialized")
    return ctx


def _save_graph(project_id: str = "default") -> None:
    bump_graph_revision(project_id)
    ws = _app_state.get("workspace")
    if ws is not None:
        ws.save_graph(project_id)


@router.post("/demo/seed", response_model=DemoSeedResponse)
async def seed_demo(request: DemoSeedRequest) -> DemoSeedResponse:
    """Load the checked-in demo corpus into the graph (idempotent)."""
    ctxflow = _resolve_ctxflow(request.project_id)

    try:
        stats = seed_demo_corpus(ctxflow, project_id=request.project_id)
        _save_graph(request.project_id)
        total = stats["papers_ingested"] + stats["papers_skipped"]
        return DemoSeedResponse(
            **stats,
            message=(
                f"Demo corpus ready: ingested {stats['papers_ingested']} paper(s) "
                f"and {stats['claims_ingested']} claim(s); skipped "
                f"{stats['papers_skipped']} of {total} already present. "
                f"{stats.get('superseded_claims', 0)} superseded and "
                f"{stats.get('contested_claims', 0)} contested claim(s)."
            ),
        )
    except Exception as exc:
        logger.error("Demo seed failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))


@router.post("/papers/ingest", response_model=PaperIngestResponse)
async def ingest_paper(request: PaperIngestRequest) -> PaperIngestResponse:
    """Ingest a paper into the knowledge graph."""
    ctxflow = _resolve_ctxflow(request.project_id)

    try:
        # Create paper reference node
        paper_content = f"Paper: {request.title}"
        if request.authors:
            paper_content += f"\nAuthors: {', '.join(request.authors[:5])}"
        if request.year:
            paper_content += f"\nYear: {request.year}"
        if request.doi:
            paper_content += f"\nDOI: {request.doi}"

        paper_tags = _title_tags(request.title)
        superseded_before = _superseded_count(ctxflow)

        paper_node_id = ctxflow.ingest(
            content=paper_content,
            node_type="paper",
            tags=paper_tags,
            metadata={
                "project_id": request.project_id,
                "title": request.title,
                "authors": request.authors,
                "year": request.year,
                "doi": request.doi,
                "arxiv_id": request.arxiv_id,
                "url": request.url,
            },
            check_contradictions=False,
        )

        # Ingest claims from abstract/content
        claim_ids: list = []
        text_to_process = request.abstract or request.content

        if text_to_process:
            sentences = re.split(r'(?<=[.!?])\s+', text_to_process)
            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) < 20:
                    continue

                claim_id = ctxflow.ingest(
                    content=sentence[:500],
                    node_type="claim",
                    source_id=paper_node_id,
                    relation_type="extracted_from",
                    tags=paper_tags,
                    metadata={
                        "project_id": request.project_id,
                        "source_paper": request.title,
                        "source_doi": request.doi,
                    },
                    check_contradictions=True,
                )
                claim_ids.append(claim_id)

        contradictions = max(
            0, _superseded_count(ctxflow) - superseded_before
        )
        _save_graph(request.project_id)

        return PaperIngestResponse(
            success=True,
            paper_node_id=paper_node_id,
            claim_node_ids=claim_ids,
            contradictions_found=contradictions,
            project_id=request.project_id,
            message=f"Ingested paper '{request.title}' with {len(claim_ids)} claims.",
        )
    except Exception as exc:
        logger.error("Paper ingest failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))


@router.post("/papers/upload", response_model=PaperIngestResponse)
async def upload_paper(
    file: UploadFile = File(...),
    project_id: str = Form("default"),
    title: str = Form(""),
) -> PaperIngestResponse:
    """Parse a PDF with the Paper Reader skill and ingest its claims."""
    ctxflow = _resolve_ctxflow(project_id)

    paper_reader = _app_state.get("paper_reader")
    if paper_reader is None:
        raise HTTPException(503, detail="Paper Reader skill not initialized")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, detail="Uploaded file is empty")

    suffix = Path(file.filename or "paper.pdf").suffix or ".pdf"
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(payload)
            temp_path = handle.name

        info = paper_reader.read_pdf(temp_path)
        extracted = paper_reader.extract_claims(temp_path)
    except ImportError as exc:
        raise HTTPException(
            503,
            detail=(
                "PDF parsing requires PyMuPDF. Install it with "
                "`pip install ctxflow[researchos]`."
            ),
        ) from exc
    except Exception as exc:
        logger.error("Paper upload failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    paper_title = title.strip() or info.get("title") or Path(
        file.filename or "Untitled paper"
    ).stem
    paper_tags = _title_tags(paper_title)
    superseded_before = _superseded_count(ctxflow)

    paper_node_id = ctxflow.ingest(
        content=f"Paper: {paper_title}\n\n{info.get('full_text', '')[:5000]}",
        node_type="paper",
        tags=paper_tags,
        metadata={
            "project_id": project_id,
            "title": paper_title,
            "filename": file.filename or "",
            "page_count": info.get("page_count", 0),
        },
        check_contradictions=False,
    )

    claim_ids = []
    for claim in extracted:
        claim_tags = paper_tags | set(claim.tags)
        claim_id = ctxflow.ingest(
            content=claim.text[:500],
            node_type=claim.claim_type,
            source_id=paper_node_id,
            relation_type="extracted_from",
            tags=claim_tags,
            metadata={
                "project_id": project_id,
                "source_paper": paper_title,
                "confidence": claim.confidence,
                "section": claim.source_section,
                "page": claim.page_number,
            },
            check_contradictions=True,
        )
        claim_ids.append(claim_id)

    contradictions = max(0, _superseded_count(ctxflow) - superseded_before)
    _save_graph(project_id)

    return PaperIngestResponse(
        success=True,
        paper_node_id=paper_node_id,
        claim_node_ids=claim_ids,
        contradictions_found=contradictions,
        project_id=project_id,
        message=(
            f"Ingested PDF '{paper_title}' with {len(claim_ids)} claims."
        ),
    )


# ---------------------------------------------------------------------------
# Graph Data
# ---------------------------------------------------------------------------

@router.get("/graph/stats", response_model=GraphStatsResponse)
async def graph_stats(project_id: str = "default") -> GraphStatsResponse:
    """Get knowledge graph statistics."""
    ctxflow = _resolve_ctxflow(project_id)

    graph = ctxflow.graph
    all_nodes = graph.all_nodes()

    status_dist: dict = {}
    type_dist: dict = {}
    for node in all_nodes:
        status = node.status if isinstance(node.status, str) else node.status.value
        status_dist[status] = status_dist.get(status, 0) + 1
        type_dist[node.node_type] = type_dist.get(node.node_type, 0) + 1

    return GraphStatsResponse(
        total_nodes=graph.node_count,
        total_edges=graph.edge_count,
        revision=get_graph_revision(project_id),
        status_distribution=status_dist,
        type_distribution=type_dist,
    )


@router.get("/graph/data", response_model=GraphDataResponse)
async def graph_data(project_id: str = "default") -> GraphDataResponse:
    """Get full graph data for visualization."""
    ctxflow = _resolve_ctxflow(project_id)

    graph = ctxflow.graph
    all_nodes = graph.all_nodes()

    nodes = []
    edges = []

    for node in all_nodes:
        nodes.append(NodeResponse(
            id=node.id,
            content=node.content[:500],
            summary=node.summary,
            node_type=node.node_type,
            status=node.status if isinstance(node.status, str) else node.status.value,
            tags=sorted(node.tags),
            superseded_by=node.superseded_by,
            metadata=node.metadata,
            timestamp=node.timestamp,
        ))

        for edge in graph.get_edges_from(node.id):
            edges.append(EdgeResponse(
                source_id=edge.source_id,
                target_id=edge.target_id,
                relation_type=edge.relation_type,
                weight=edge.weight,
                metadata=edge.metadata,
            ))

    return GraphDataResponse(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Research Trace
# ---------------------------------------------------------------------------

@router.post("/trace", response_model=TraceResponse)
async def research_trace(request: TraceRequest, project_id: str = "default") -> TraceResponse:
    """Build a research trace for a node."""
    ctxflow = _resolve_ctxflow(project_id)

    try:
        trace = ctxflow.research_trace(request.node_id)

        chain_steps = []
        for step in trace.supporting_chain.steps:
            chain_steps.append(TraceStepResponse(
                node_id=step.node.id,
                content=step.node.content[:500],
                summary=step.node.summary,
                node_type=step.node.node_type,
                status=step.node.status if isinstance(step.node.status, str) else step.node.status.value,
                edge_relation=step.edge.relation_type if step.edge else None,
                edge_weight=step.edge.weight if step.edge else None,
            ))

        superseded = [
            NodeResponse(
                id=n.id,
                content=n.content[:500],
                summary=n.summary,
                node_type=n.node_type,
                status=n.status if isinstance(n.status, str) else n.status.value,
                superseded_by=n.superseded_by,
            )
            for n in trace.superseded_claims
        ]

        contradictions = [
            {
                "node_id": n.id,
                "content": n.content[:500],
                "relation": e.relation_type,
                "confidence": e.metadata.get("confidence", 0.0),
                "evidence": e.metadata.get("evidence", ""),
            }
            for n, e in trace.contradicting_claims
        ]

        sources = [
            NodeResponse(
                id=n.id,
                content=n.content[:500],
                summary=n.summary,
                node_type=n.node_type,
                status=n.status if isinstance(n.status, str) else n.status.value,
                tags=sorted(n.tags),
            )
            for n in trace.source_papers
        ]

        return TraceResponse(
            answer_node_id=trace.answer_node_id,
            provenance_chain=chain_steps,
            superseded_claims=superseded,
            contradictions=contradictions,
            source_papers=sources,
        )
    except Exception as exc:
        logger.error("Trace endpoint failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))
