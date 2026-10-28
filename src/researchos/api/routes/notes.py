"""Notes Routes — lightweight researcher notes inside a project workspace."""

from __future__ import annotations

import logging
from typing import Any, List

from fastapi import APIRouter, HTTPException

from researchos.api.graph_revision import bump_graph_revision
from researchos.api.schemas import (
    NoteCreateRequest,
    NoteListResponse,
    NoteResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["notes"])


_app_state: dict = {}


def set_app_state(state: dict) -> None:
    """Inject application state."""
    global _app_state
    _app_state = state


def _to_response(node: Any) -> NoteResponse:
    metadata = node.metadata or {}
    return NoteResponse(
        node_id=node.id,
        content=node.content,
        title=str(metadata.get("title", "")),
        project_id=str(metadata.get("project_id", "default")),
        tags=sorted(node.tags),
        timestamp=node.timestamp,
    )


def _resolve_ctxflow(project_id: str) -> Any:
    workspace = _app_state.get("workspace")
    if workspace is not None:
        return workspace.get_graph(project_id)
    ctxflow = _app_state.get("ctxflow")
    if ctxflow is None:
        raise HTTPException(503, detail="CtxFlow not initialized")
    return ctxflow


@router.post("/notes", response_model=NoteResponse)
async def create_note(request: NoteCreateRequest) -> NoteResponse:
    """Store a researcher note as a CtxFlow node."""
    ctxflow = _resolve_ctxflow(request.project_id)

    title = request.title.strip()
    content = request.content.strip()
    if title:
        content = f"{title}\n\n{content}"

    tags = {tag.strip().lower() for tag in request.tags if tag.strip()}
    tags.add("note")

    try:
        node_id = ctxflow.ingest(
            content=content,
            node_type="note",
            tags=tags,
            metadata={
                "project_id": request.project_id,
                "title": title,
            },
            check_contradictions=False,
        )
        node = ctxflow.graph.get_node(node_id)
        bump_graph_revision(request.project_id)
        workspace = _app_state.get("workspace")
        if workspace is not None:
            workspace.save_graph(request.project_id)
        return _to_response(node)
    except Exception as exc:
        logger.error("Note creation failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=str(exc))


@router.get("/notes", response_model=NoteListResponse)
async def list_notes(project_id: str = "default") -> NoteListResponse:
    """List notes for a project (newest first)."""
    ctxflow = _resolve_ctxflow(project_id)

    notes: List[NoteResponse] = []
    for node in ctxflow.graph.all_nodes():
        if node.node_type != "note":
            continue
        if str((node.metadata or {}).get("project_id", "default")) != project_id:
            continue
        notes.append(_to_response(node))

    notes.sort(key=lambda note: note.timestamp, reverse=True)
    return NoteListResponse(notes=notes, total=len(notes))
