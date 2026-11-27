"""Dataset, analysis and chart routes for the research workstation."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from researchos.analytics import MAX_DATASET_BYTES, TRANSFORM_OPS, operation_catalog
from researchos.persistence import sanitize_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["datasets"])

_app_state: dict = {}


def set_app_state(state: dict) -> None:
    global _app_state
    _app_state = state


def workstation() -> Any:
    engine = _app_state.get("workstation")
    if engine is None:
        raise HTTPException(503, detail="Workstation engines are not initialized.")
    return engine


def project(project_id: str) -> str:
    try:
        return sanitize_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from None


def fail(exc: Exception) -> HTTPException:
    """Map engine validation errors to 4xx without hiding the reason."""
    message = str(exc)
    status = 404 if "not found" in message.lower() else 400
    return HTTPException(status, detail=message)


class TransformRequest(BaseModel):
    project_id: str = "default"
    steps: List[Dict[str, Any]] = Field(min_length=1)
    version: Optional[int] = Field(default=None, ge=1)


class AnalysisRequest(BaseModel):
    project_id: str = "default"
    dataset_id: str
    operation: str
    params: Dict[str, Any] = Field(default_factory=dict)
    version: Optional[int] = Field(default=None, ge=1)


class ChartRequest(BaseModel):
    project_id: str = "default"
    spec: Dict[str, Any]


@router.get("/analytics/operations")
def list_operations() -> Dict[str, Any]:
    return {"operations": operation_catalog(), "transform_ops": list(TRANSFORM_OPS)}


@router.post("/datasets/upload")
async def upload_dataset(
    file: UploadFile = File(...),
    project_id: str = Form("default"),
    name: str = Form(""),
    description: str = Form(""),
    delimiter: str = Form(""),
    sheet: str = Form(""),
    column_types: str = Form(""),
) -> Dict[str, Any]:
    pid = project(project_id)
    payload = await file.read(MAX_DATASET_BYTES + 1)
    types: Optional[Dict[str, str]] = None
    if column_types.strip():
        try:
            types = json.loads(column_types)
        except json.JSONDecodeError:
            raise HTTPException(400, detail="column_types must be a JSON object.") from None
        if not isinstance(types, dict):
            raise HTTPException(400, detail="column_types must be a JSON object.")
    try:
        return await run_in_threadpool(
            workstation().datasets.ingest,
            pid,
            name=name or (file.filename or "dataset"),
            filename=file.filename or "dataset.csv",
            payload=payload,
            delimiter=delimiter or None,
            sheet=sheet or None,
            column_types=types,
            description=description,
        )
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/datasets")
def list_datasets(project_id: str = "default") -> Dict[str, Any]:
    datasets = workstation().datasets.list(project(project_id))
    return {"datasets": datasets, "total": len(datasets)}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, project_id: str = "default", version: Optional[int] = None) -> Dict[str, Any]:
    engine = workstation()
    pid = project(project_id)
    try:
        return {"dataset": engine.datasets.get(pid, dataset_id), "version": engine.datasets.version(pid, dataset_id, version)}
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/datasets/{dataset_id}/preview")
def preview_dataset(
    dataset_id: str, project_id: str = "default", version: Optional[int] = None,
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    try:
        return workstation().datasets.preview(project(project_id), dataset_id, version, limit)
    except ValueError as exc:
        raise fail(exc) from None


@router.post("/datasets/{dataset_id}/transform")
def transform_dataset(dataset_id: str, request: TransformRequest) -> Dict[str, Any]:
    try:
        return workstation().datasets.transform(
            project(request.project_id), dataset_id, request.steps, version=request.version
        )
    except ValueError as exc:
        raise fail(exc) from None


@router.post("/analyses")
def run_analysis(request: AnalysisRequest) -> Dict[str, Any]:
    try:
        return workstation().analyses.run(
            project(request.project_id), request.dataset_id, request.operation, request.params,
            version=request.version,
        )
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/analyses")
def list_analyses(project_id: str = "default", dataset_id: Optional[str] = None) -> Dict[str, Any]:
    records = workstation().analyses.list(project(project_id), dataset_id)
    return {"analyses": records, "total": len(records)}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str, project_id: str = "default") -> Dict[str, Any]:
    try:
        return workstation().analyses.get(project(project_id), analysis_id)
    except ValueError as exc:
        raise fail(exc) from None


@router.post("/charts")
def create_chart(request: ChartRequest) -> Dict[str, Any]:
    try:
        return workstation().charts.create(project(request.project_id), request.spec)
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/charts")
def list_charts(project_id: str = "default", dataset_id: Optional[str] = None) -> Dict[str, Any]:
    records = workstation().charts.list(project(project_id), dataset_id)
    return {"charts": records, "total": len(records)}


@router.get("/charts/{chart_id}")
def get_chart(chart_id: str, project_id: str = "default") -> Dict[str, Any]:
    try:
        return workstation().charts.get(project(project_id), chart_id)
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/charts/{chart_id}/image")
def chart_image(chart_id: str, project_id: str = "default", format: str = "png") -> FileResponse:
    try:
        path = workstation().charts.file_path(project(project_id), chart_id, format)
    except ValueError as exc:
        raise fail(exc) from None
    media = "image/svg+xml" if format == "svg" else "image/png"
    return FileResponse(
        path,
        media_type=media,
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
    )
