"""Project listing and creation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from researchos.api.schemas import (
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectResponse,
)
from researchos.persistence import ResearchWorkspace, sanitize_project_id

router = APIRouter(prefix="/api", tags=["projects"])

_app_state: dict = {}


def set_app_state(state: dict) -> None:
    global _app_state
    _app_state = state


def _workspace() -> ResearchWorkspace:
    workspace = _app_state.get("workspace")
    if workspace is None:
        raise HTTPException(503, detail="Workspace not initialized")
    return workspace


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects() -> ProjectListResponse:
    workspace = _workspace()
    items = []
    for meta in workspace.list_projects():
        graph = workspace.get_graph(meta.project_id)
        items.append(
            ProjectResponse(
                project_id=meta.project_id,
                name=meta.name,
                description=meta.description,
                created_at=meta.created_at,
                updated_at=meta.updated_at,
                nodes=graph.graph.node_count,
                edges=graph.graph.edge_count,
            )
        )
    return ProjectListResponse(projects=items)


@router.post("/projects", response_model=ProjectResponse)
async def create_project(request: ProjectCreateRequest) -> ProjectResponse:
    try:
        project_id = sanitize_project_id(request.project_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    workspace = _workspace()
    meta = workspace.create_project(
        project_id, name=request.name, description=request.description
    )
    graph = workspace.get_graph(meta.project_id)
    return ProjectResponse(
        project_id=meta.project_id,
        name=meta.name,
        description=meta.description,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        nodes=graph.graph.node_count,
        edges=graph.graph.edge_count,
    )
