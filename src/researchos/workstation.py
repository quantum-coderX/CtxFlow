"""Workstation engines bundled for one ResearchOS application.

:class:`Workstation` owns the project-scoped engines (datasets, analyses,
charts, sources, artifacts, documents/OCR, proofs) and registers each one as
a typed, allowlisted skill so the :class:`~researchos.harness.ToolHarness`
can run them in validated dependency plans. Models may *request* these
operations; only the engines execute them.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from researchos.analytics import (
    AnalysisEngine,
    DatasetRegistry,
    MAX_DATASET_BYTES,
    TRANSFORM_OPS,
    operation_catalog,
)
from researchos.artifacts import (
    ArtifactEngine,
    Compiler,
    TEMPLATES,
    compose_report_spec,
    detect_latex_engine,
)
from researchos.charts import ChartEngine
from researchos.ocr import DocumentStore, engine_availability
from researchos.persistence import sanitize_project_id
from researchos.project_store import ProjectStore
from researchos.proof import ProofChecker
from researchos.sources import SourceRegistry

MAX_INLINE_DATASET_CHARS = 1_000_000

GraphResolver = Callable[[str], Any]


class Workstation:
    """Engines sharing one project store."""

    def __init__(self, root: Path, *, compiler: Optional[Compiler] = None) -> None:
        self.root = Path(root)
        self.store = ProjectStore(self.root)
        self.datasets = DatasetRegistry(self.store)
        self.analyses = AnalysisEngine(self.datasets)
        self.charts = ChartEngine(self.datasets, self.analyses)
        self.sources = SourceRegistry(self.store)
        self.artifacts = ArtifactEngine(self.store, self.sources, self.analyses, self.charts, compiler=compiler)
        self.documents = DocumentStore(self.store)
        self.proofs = ProofChecker(self.store)

    def capabilities(self) -> Dict[str, Any]:
        """Report what is actually usable, not merely configured."""
        import matplotlib
        import numpy
        import scipy

        def has(module: str) -> bool:
            try:
                __import__(module)
                return True
            except ImportError:
                return False

        latex = detect_latex_engine()
        return {
            "python": platform.python_version(),
            "datasets": {
                "available": True,
                "formats": {
                    "csv": True, "tsv": True, "json": True, "jsonl": True,
                    "xlsx": has("openpyxl"), "parquet": has("pyarrow"),
                },
                "max_bytes": MAX_DATASET_BYTES,
                "transform_ops": list(TRANSFORM_OPS),
            },
            "analytics": {
                "available": True,
                "operations": [op["name"] for op in operation_catalog()],
                "numpy": numpy.__version__,
                "scipy": scipy.__version__,
            },
            "charts": {"available": True, "renderer": f"matplotlib {matplotlib.__version__}",
                       "types": ["line", "bar", "scatter", "histogram", "box", "heatmap"]},
            "artifacts": {
                "available": True,
                "templates": sorted(TEMPLATES),
                "formats": {"markdown": True, "html": True, "latex": True, "pdf": latex is not None},
                "latex_engine": latex[0] if latex else None,
            },
            "ocr": engine_availability(),
            "proof": self.proofs.availability(),
        }

    # ------------------------------------------------------------------
    # Skill registration
    # ------------------------------------------------------------------

    def register_skills(self, registry: Any, *, graph_for: GraphResolver, llm: Any = None) -> List[str]:
        """Register engine skills that are not already present; return their names."""

        def pid(project_id: str) -> str:
            return sanitize_project_id(project_id)

        def dataset_ingest(project_id: str, name: str, content: str, format: str = "csv",
                           description: str = "") -> Dict[str, Any]:
            if len(content) > MAX_INLINE_DATASET_CHARS:
                raise ValueError(f"Inline dataset content exceeds {MAX_INLINE_DATASET_CHARS} characters.")
            if format not in ("csv", "tsv", "json", "jsonl"):
                raise ValueError("Inline datasets support csv, tsv, json and jsonl.")
            return self.datasets.ingest(
                pid(project_id), name=name, filename=f"{name}.{format}", payload=content.encode("utf-8"),
                fmt=format, source="tool", description=description,
            )

        def dataset_profile(project_id: str, dataset_id: str, version: Optional[int] = None) -> Dict[str, Any]:
            return self.datasets.version(pid(project_id), dataset_id, version)

        def dataset_transform(project_id: str, dataset_id: str, steps: List[Dict[str, Any]],
                              version: Optional[int] = None) -> Dict[str, Any]:
            return self.datasets.transform(pid(project_id), dataset_id, steps, version=version)

        def dataset_analyze(project_id: str, dataset_id: str, operation: str,
                            params: Optional[Dict[str, Any]] = None, version: Optional[int] = None) -> Dict[str, Any]:
            return self.analyses.run(pid(project_id), dataset_id, operation, params,
                                     version=version, requested_by="tool")

        def create_chart(project_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
            return self.charts.create(pid(project_id), spec, requested_by="tool")

        def proof_check(source: str, declaration: Optional[str] = None, project: Optional[str] = None,
                        project_id: Optional[str] = None) -> Dict[str, Any]:
            request = {"source": source, "declaration": declaration, "project": project}
            return self.proofs.check(request, project_id=pid(project_id) if project_id else None)

        def academic_ocr(project_id: str, document_id: str, engine: str = "auto",
                         max_pages: int = 50) -> Dict[str, Any]:
            return self.documents.extract(pid(project_id), document_id,
                                          {"engine": engine, "max_pages": max_pages})

        def document_compose(project_id: str, topic: str = "", title: str = "",
                             template: str = "literature_review", analysis_ids: Optional[List[str]] = None,
                             chart_ids: Optional[List[str]] = None, max_sources: int = 12,
                             build: bool = True, formats: Optional[List[str]] = None) -> Dict[str, Any]:
            project = pid(project_id)
            graph = graph_for(project)
            spec, warnings = compose_report_spec(
                project, graph, topic=topic, title=title, template=template,
                analysis_ids=analysis_ids or [], chart_ids=chart_ids or [],
                max_sources=max_sources, llm=llm, formats=formats,
            )
            record = self.artifacts.create(project, graph, spec, build=build, requested_by="tool")
            return {**record, "compose_warnings": warnings}

        def latex_compile(project_id: str, artifact_id: str, refresh_inputs: bool = False) -> Dict[str, Any]:
            project = pid(project_id)
            record = self.artifacts.build(project, artifact_id, graph_for(project),
                                          refresh_inputs=refresh_inputs, formats=["latex", "pdf"])
            return {"artifact_id": artifact_id, "status": record["status"], "build": record["builds"][-1]}

        obj = {"type": "object"}
        specs: List[Dict[str, Any]] = [
            {
                "name": "dataset_ingest", "fn": dataset_ingest, "tags": ["dataset", "ingest"],
                "description": "Register small inline tabular data (CSV/TSV/JSON) as a versioned project dataset.",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "name": {"type": "string"}, "content": {"type": "string"},
                    "format": {"type": "string", "enum": ["csv", "tsv", "json", "jsonl"]},
                    "description": {"type": "string"}},
                    "required": ["project_id", "name", "content"]},
            },
            {
                "name": "dataset_profile", "fn": dataset_profile, "tags": ["dataset", "profile"],
                "description": "Return the schema, quality profile and provenance of a dataset version.",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "dataset_id": {"type": "string"},
                    "version": {"type": ["integer", "null"], "minimum": 1}},
                    "required": ["project_id", "dataset_id"]},
            },
            {
                "name": "dataset_transform", "fn": dataset_transform, "tags": ["dataset", "transform"],
                "description": f"Apply validated cleaning steps ({', '.join(TRANSFORM_OPS)}) producing a new version.",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "dataset_id": {"type": "string"},
                    "steps": {"type": "array"}, "version": {"type": ["integer", "null"], "minimum": 1}},
                    "required": ["project_id", "dataset_id", "steps"]},
            },
            {
                "name": "dataset_analyze", "fn": dataset_analyze, "tags": ["analytics", "statistics"],
                "description": "Run one named operation from the fixed analytics catalog on a dataset version.",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "dataset_id": {"type": "string"},
                    "operation": {"type": "string", "enum": [op["name"] for op in operation_catalog()]},
                    "params": {"type": ["object", "null"]}, "version": {"type": ["integer", "null"], "minimum": 1}},
                    "required": ["project_id", "dataset_id", "operation"]},
            },
            {
                "name": "create_chart", "fn": create_chart, "tags": ["visualization", "chart"],
                "description": "Render a chart from a validated chart spec over a dataset version or analysis.",
                "input_schema": {**obj, "properties": {"project_id": {"type": "string"}, "spec": obj},
                                 "required": ["project_id", "spec"]},
            },
            {
                "name": "proof_check", "fn": proof_check, "tags": ["proof", "lean", "formal"],
                "description": "Elaborate Lean 4 source with a bounded checker; reports verified/failed/unavailable.",
                "input_schema": {**obj, "properties": {
                    "source": {"type": "string"}, "declaration": {"type": ["string", "null"]},
                    "project": {"type": ["string", "null"]}, "project_id": {"type": ["string", "null"]}},
                    "required": ["source"]},
            },
            {
                "name": "academic_ocr", "fn": academic_ocr, "tags": ["ocr", "pdf", "ingestion"],
                "description": "Extract page-located text from a stored project PDF (text layer, Tesseract or Nougat).",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "document_id": {"type": "string"},
                    "engine": {"type": "string", "enum": ["auto", "text_layer", "tesseract", "nougat"]},
                    "max_pages": {"type": "integer", "minimum": 1}},
                    "required": ["project_id", "document_id"]},
            },
            {
                "name": "document_compose", "fn": document_compose, "tags": ["artifact", "report", "writing"],
                "description": "Compose and build a cited report from workspace sources, claims, analyses and charts.",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "topic": {"type": "string"}, "title": {"type": "string"},
                    "template": {"type": "string", "enum": sorted(TEMPLATES)},
                    "analysis_ids": {"type": "array"}, "chart_ids": {"type": "array"},
                    "max_sources": {"type": "integer", "minimum": 1}, "build": {"type": "boolean"},
                    "formats": {"type": ["array", "null"]}},
                    "required": ["project_id"]},
            },
            {
                "name": "latex_compile", "fn": latex_compile, "tags": ["artifact", "latex", "pdf"],
                "description": "Rebuild an artifact's LaTeX and compile it to PDF in an isolated subprocess.",
                "input_schema": {**obj, "properties": {
                    "project_id": {"type": "string"}, "artifact_id": {"type": "string"},
                    "refresh_inputs": {"type": "boolean"}},
                    "required": ["project_id", "artifact_id"]},
            },
        ]
        registered = []
        for spec in specs:
            if registry.has_skill(spec["name"]):
                continue
            registry.register_function(
                name=spec["name"], fn=spec["fn"], description=spec["description"],
                input_schema=spec["input_schema"], output_schema={"type": "object"}, tags=spec["tags"],
            )
            registered.append(spec["name"])
        return registered
