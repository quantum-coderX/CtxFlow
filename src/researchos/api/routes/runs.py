"""Runs API — list and retrieve saved research run records."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from researchos.api.schemas import RunListResponse, RunResponse

router = APIRouter(prefix="/api", tags=["runs"])

_app_state: dict = {}


def set_app_state(state: dict) -> None:
    global _app_state
    _app_state = state


def _workspace():
    ws = _app_state.get("workspace")
    if ws is None:
        raise HTTPException(503, detail="Workspace not initialized")
    return ws


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 50,
) -> RunListResponse:
    """List saved research runs, optionally filtered by project and session."""
    ws = _workspace()
    records = ws.list_runs(
        project_id=project_id,
        limit=limit,
        session_id=session_id,
    )
    runs = []
    for rec in records:
        try:
            valid_keys = set(RunResponse.model_fields.keys())
            runs.append(RunResponse(**{k: v for k, v in rec.items() if k in valid_keys}))
        except Exception:
            # Corrupt or partial record — include basic info only
            runs.append(
                RunResponse(
                    run_id=str(rec.get("run_id") or "unknown"),
                    project_id=str(rec.get("project_id") or "default"),
                    query=str(rec.get("query") or ""),
                    status=str(rec.get("status") or "unknown"),
                )
            )
    return RunListResponse(runs=runs)


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str) -> RunResponse:
    """Retrieve a single research run record by ID."""
    ws = _workspace()
    rec = ws.load_run(run_id)
    if rec is None:
        raise HTTPException(404, detail=f"Run {run_id!r} not found")
    try:
        valid_keys = set(RunResponse.model_fields.keys())
        return RunResponse(**{k: v for k, v in rec.items() if k in valid_keys})
    except Exception as exc:
        raise HTTPException(500, detail=f"Could not deserialize run record: {exc}") from exc
