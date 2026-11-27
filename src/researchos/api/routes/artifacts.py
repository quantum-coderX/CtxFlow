"""Source registry, artifact, document/OCR, proof and capability routes."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from researchos.api.routes.datasets import fail, project, workstation
from researchos.artifacts import compose_report_spec
from researchos.ocr import MAX_DOCUMENT_BYTES
from researchos.sources import bibtex

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["artifacts"])

_app_state: dict = {}

_MEDIA = {
    "markdown": ("text/markdown; charset=utf-8", "report.md"),
    "html": ("text/html; charset=utf-8", "report.html"),
    "latex": ("text/x-tex; charset=utf-8", "main.tex"),
    "bibtex": ("text/x-bibtex; charset=utf-8", "refs.bib"),
    "pdf": ("application/pdf", "report.pdf"),
    "spec": ("application/json", "spec.json"),
    "log": ("text/plain; charset=utf-8", "build.log"),
}


def set_app_state(state: dict) -> None:
    global _app_state
    _app_state = state


def _graph(project_id: str) -> Any:
    workspace = _app_state.get("workspace")
    if workspace is not None:
        return workspace.get_graph(project_id)
    ctxflow = _app_state.get("ctxflow")
    if ctxflow is None:
        raise HTTPException(503, detail="CtxFlow not initialized")
    return ctxflow


class ArtifactCreateRequest(BaseModel):
    project_id: str = "default"
    spec: Dict[str, Any]
    build: bool = True


class ComposeRequest(BaseModel):
    project_id: str = "default"
    topic: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=300)
    template: Literal["literature_review", "technical_report", "paper_summary", "manuscript"] = "literature_review"
    authors: List[str] = Field(default_factory=list, max_length=50)
    analysis_ids: List[str] = Field(default_factory=list, max_length=50)
    chart_ids: List[str] = Field(default_factory=list, max_length=50)
    max_sources: int = Field(default=12, ge=1, le=50)
    use_model: bool = False
    formats: Optional[List[Literal["markdown", "html", "latex", "pdf"]]] = None
    build: bool = True


class BuildRequest(BaseModel):
    project_id: str = "default"
    refresh_inputs: bool = False
    formats: Optional[List[Literal["markdown", "html", "latex", "pdf"]]] = None


class SpecUpdateRequest(BaseModel):
    project_id: str = "default"
    spec: Dict[str, Any]


class OcrRequestBody(BaseModel):
    project_id: str = "default"
    engine: Literal["auto", "text_layer", "tesseract", "nougat"] = "auto"
    max_pages: int = Field(default=50, ge=1, le=500)


class ProofRequest(BaseModel):
    project_id: Optional[str] = "default"
    source: str = Field(min_length=1)
    declaration: Optional[str] = None
    project: Optional[str] = None
    timeout_s: float = Field(default=120.0, ge=1.0, le=600.0)


@router.get("/workstation/capabilities")
def capabilities() -> Dict[str, Any]:
    return workstation().capabilities()


# -- Sources -----------------------------------------------------------------


@router.get("/sources")
def list_sources(project_id: str = "default") -> Dict[str, Any]:
    pid = project(project_id)
    records = workstation().sources.sync(pid, _graph(pid))
    return {"sources": records, "total": len(records)}


@router.get("/sources/bibtex", response_class=PlainTextResponse)
def sources_bibtex(project_id: str = "default") -> PlainTextResponse:
    pid = project(project_id)
    return PlainTextResponse(bibtex(workstation().sources.sync(pid, _graph(pid))), media_type="text/x-bibtex")


# -- Artifacts ---------------------------------------------------------------


@router.post("/artifacts")
def create_artifact(request: ArtifactCreateRequest) -> Dict[str, Any]:
    pid = project(request.project_id)
    try:
        return workstation().artifacts.create(pid, _graph(pid), request.spec, build=request.build)
    except ValueError as exc:
        raise fail(exc) from None


@router.post("/artifacts/compose")
def compose_artifact(request: ComposeRequest) -> Dict[str, Any]:
    pid = project(request.project_id)
    graph = _graph(pid)
    llm = _app_state.get("model_gateway") if request.use_model else None
    if request.use_model and llm is None:
        raise HTTPException(409, detail="use_model requested but no model gateway is configured.")
    try:
        spec, warnings = compose_report_spec(
            pid, graph, topic=request.topic, title=request.title, template=request.template,
            authors=request.authors, max_sources=request.max_sources,
            analysis_ids=request.analysis_ids, chart_ids=request.chart_ids, llm=llm,
            formats=list(request.formats) if request.formats else None,
        )
        record = workstation().artifacts.create(pid, graph, spec, build=request.build)
    except ValueError as exc:
        raise fail(exc) from None
    return {**record, "compose_warnings": warnings}


@router.get("/artifacts")
def list_artifacts(project_id: str = "default") -> Dict[str, Any]:
    records = workstation().artifacts.list(project(project_id))
    return {"artifacts": records, "total": len(records)}


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, project_id: str = "default") -> Dict[str, Any]:
    try:
        return workstation().artifacts.get(project(project_id), artifact_id)
    except ValueError as exc:
        raise fail(exc) from None


@router.put("/artifacts/{artifact_id}/spec")
def update_artifact_spec(artifact_id: str, request: SpecUpdateRequest) -> Dict[str, Any]:
    try:
        return workstation().artifacts.update_spec(project(request.project_id), artifact_id, request.spec)
    except ValueError as exc:
        raise fail(exc) from None


@router.post("/artifacts/{artifact_id}/build")
def build_artifact(artifact_id: str, request: BuildRequest) -> Dict[str, Any]:
    pid = project(request.project_id)
    try:
        return workstation().artifacts.build(
            pid, artifact_id, _graph(pid), refresh_inputs=request.refresh_inputs,
            formats=list(request.formats) if request.formats else None,
        )
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/artifacts/{artifact_id}/staleness")
def artifact_staleness(artifact_id: str, project_id: str = "default") -> Dict[str, Any]:
    pid = project(project_id)
    try:
        return workstation().artifacts.staleness(pid, artifact_id, _graph(pid))
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/artifacts/{artifact_id}/files/{fmt}")
def artifact_file(
    artifact_id: str, fmt: str, project_id: str = "default", version: Optional[int] = None,
    download: bool = False,
) -> FileResponse:
    if fmt not in _MEDIA:
        raise HTTPException(400, detail=f"fmt must be one of {sorted(_MEDIA)}.")
    try:
        path = workstation().artifacts.file_path(project(project_id), artifact_id, fmt, version)
    except ValueError as exc:
        raise fail(exc) from None
    media, filename = _MEDIA[fmt]
    # HTML is always served as an attachment so untrusted report text never runs on this origin.
    as_attachment = download or fmt in ("html", "pdf")
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'"}
    if as_attachment:
        return FileResponse(path, media_type=media, filename=f"{artifact_id}-{filename}", headers=headers)
    return FileResponse(path, media_type=media, headers=headers)


# -- Documents / OCR -----------------------------------------------------------


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    project_id: str = Form("default"),
    extract: bool = Form(True),
    engine: str = Form("auto"),
) -> Dict[str, Any]:
    pid = project(project_id)
    payload = await file.read(MAX_DOCUMENT_BYTES + 1)
    engine_store = workstation().documents
    try:
        record = await run_in_threadpool(
            engine_store.add, pid, filename=file.filename or "document.pdf", payload=payload
        )
        result: Dict[str, Any] = {"document": record}
        if extract:
            result["extraction"] = await run_in_threadpool(
                engine_store.extract, pid, record["document_id"], {"engine": engine}
            )
        return result
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/documents")
def list_documents(project_id: str = "default") -> Dict[str, Any]:
    records = workstation().documents.list(project(project_id))
    return {"documents": records, "total": len(records)}


@router.post("/documents/{document_id}/ocr")
def run_ocr(document_id: str, request: OcrRequestBody) -> Dict[str, Any]:
    try:
        return workstation().documents.extract(
            project(request.project_id), document_id, {"engine": request.engine, "max_pages": request.max_pages}
        )
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/documents/{document_id}/extraction")
def get_extraction(document_id: str, project_id: str = "default", number: Optional[int] = None) -> Dict[str, Any]:
    try:
        return workstation().documents.extraction(project(project_id), document_id, number)
    except ValueError as exc:
        raise fail(exc) from None


# -- Proofs ------------------------------------------------------------------


@router.post("/proofs/check")
def check_proof(request: ProofRequest) -> Dict[str, Any]:
    pid = project(request.project_id) if request.project_id else None
    try:
        return workstation().proofs.check(
            {"source": request.source, "declaration": request.declaration,
             "project": request.project, "timeout_s": request.timeout_s},
            project_id=pid,
        )
    except ValueError as exc:
        raise fail(exc) from None


@router.get("/proofs")
def list_proofs(project_id: str = "default") -> Dict[str, Any]:
    records = workstation().proofs.list(project(project_id))
    return {"proofs": records, "total": len(records)}
