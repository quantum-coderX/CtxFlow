"""Project-scoped research memory persistence.

ResearchOS keeps the **CtxFlow graph** as the single source of truth for
papers, claims, and edges. PostgreSQL/`ctxflow.memory` is a parallel event
store and is **not** used as the research graph backend.

Each project is a separate CtxFlow graph serialized with the existing JSON
export/import path so a process restart reloads the same node IDs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ctxflow import CtxFlow

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_PROJECT_ID = "default"


def data_dir_from_env(env: Optional[Dict[str, str]] = None) -> Path:
    values = env if env is not None else os.environ
    raw = str(values.get("RESEARCHOS_DATA_DIR") or ".researchos_data").strip()
    return Path(raw).expanduser()


def sanitize_project_id(project_id: str) -> str:
    value = (project_id or DEFAULT_PROJECT_ID).strip() or DEFAULT_PROJECT_ID
    if not _SAFE_ID.match(value):
        raise ValueError(
            "project_id must be 1–64 characters of letters, digits, "
            "dot, underscore, or hyphen"
        )
    return value


@dataclass
class ProjectMeta:
    project_id: str
    name: str
    created_at: float
    updated_at: float
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectMeta":
        now = time.time()
        return cls(
            project_id=str(data.get("project_id") or DEFAULT_PROJECT_ID),
            name=str(data.get("name") or data.get("project_id") or DEFAULT_PROJECT_ID),
            created_at=float(data.get("created_at") or now),
            updated_at=float(data.get("updated_at") or now),
            description=str(data.get("description") or ""),
        )


class ResearchWorkspace:
    """Load, isolate, and persist per-project CtxFlow graphs plus run records."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        *,
        injected_ctxflow: Optional[CtxFlow] = None,
        persist: Optional[bool] = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else data_dir_from_env()
        self._injected = injected_ctxflow
        self.persist = bool(persist) if persist is not None else injected_ctxflow is None
        self._lock = threading.RLock()
        self._graphs: Dict[str, CtxFlow] = {}
        self._meta: Dict[str, ProjectMeta] = {}
        if self.persist:
            (self.data_dir / "projects").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "runs").mkdir(parents=True, exist_ok=True)
            self._load_index()
        if injected_ctxflow is not None:
            self._graphs[DEFAULT_PROJECT_ID] = injected_ctxflow
            self._meta.setdefault(
                DEFAULT_PROJECT_ID,
                ProjectMeta(
                    project_id=DEFAULT_PROJECT_ID,
                    name="Default",
                    created_at=time.time(),
                    updated_at=time.time(),
                ),
            )

    @property
    def backend(self) -> str:
        if not self.persist:
            return "memory"
        return "ctxflow-json"

    def _project_dir(self, project_id: str) -> Path:
        return self.data_dir / "projects" / project_id

    def _graph_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "graph.json"

    def _meta_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "meta.json"

    def _load_index(self) -> None:
        root = self.data_dir / "projects"
        if not root.exists():
            return
        for child in root.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if meta_path.exists():
                try:
                    payload = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta = ProjectMeta.from_dict(payload)
                    self._meta[meta.project_id] = meta
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    logger.warning("Skipping corrupt project meta %s: %s", meta_path, exc)

    def list_projects(self) -> List[ProjectMeta]:
        with self._lock:
            if DEFAULT_PROJECT_ID not in self._meta:
                self.create_project(DEFAULT_PROJECT_ID, name="Default")
            return sorted(self._meta.values(), key=lambda item: item.updated_at, reverse=True)

    def create_project(self, project_id: str, *, name: str = "", description: str = "") -> ProjectMeta:
        project_id = sanitize_project_id(project_id)
        with self._lock:
            existing = self._meta.get(project_id)
            if existing is not None:
                if name:
                    existing.name = name
                if description:
                    existing.description = description
                existing.updated_at = time.time()
                self._write_meta(existing)
                return existing
            now = time.time()
            meta = ProjectMeta(
                project_id=project_id,
                name=name or project_id,
                created_at=now,
                updated_at=now,
                description=description,
            )
            self._meta[project_id] = meta
            self.get_graph(project_id)
            self._write_meta(meta)
            return meta

    def get_graph(self, project_id: str = DEFAULT_PROJECT_ID) -> CtxFlow:
        project_id = sanitize_project_id(project_id)
        with self._lock:
            if self._injected is not None:
                return self._injected
            cached = self._graphs.get(project_id)
            if cached is not None:
                return cached
            graph = CtxFlow()
            path = self._graph_path(project_id)
            if self.persist and path.exists():
                try:
                    graph.import_(str(path), format="json")
                except Exception as exc:  # noqa: BLE001 — corrupt file must not crash startup
                    logger.error("Failed to load graph for %s: %s", project_id, exc)
                    graph = CtxFlow()
            self._graphs[project_id] = graph
            if project_id not in self._meta:
                now = time.time()
                self._meta[project_id] = ProjectMeta(
                    project_id=project_id,
                    name=project_id,
                    created_at=now,
                    updated_at=now,
                )
                self._write_meta(self._meta[project_id])
            return graph

    def save_graph(self, project_id: str = DEFAULT_PROJECT_ID) -> None:
        if not self.persist or self._injected is not None:
            return
        project_id = sanitize_project_id(project_id)
        with self._lock:
            graph = self._graphs.get(project_id)
            if graph is None:
                return
            folder = self._project_dir(project_id)
            folder.mkdir(parents=True, exist_ok=True)
            graph.export(str(self._graph_path(project_id)), format="json")
            meta = self._meta.get(project_id)
            if meta is not None:
                meta.updated_at = time.time()
                self._write_meta(meta)

    def _write_meta(self, meta: ProjectMeta) -> None:
        if not self.persist:
            return
        folder = self._project_dir(meta.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        self._meta_path(meta.project_id).write_text(
            json.dumps(meta.to_dict(), indent=2),
            encoding="utf-8",
        )

    def save_run(self, record: Dict[str, Any]) -> None:
        if not self.persist:
            return
        run_id = str(record.get("run_id") or "")
        if not run_id:
            return
        path = self.data_dir / "runs" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(record, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temp_path, path)

    def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        if not self.persist:
            return None
        path = self.data_dir / "runs" / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def conversation_history(
        self,
        project_id: str,
        session_id: str,
        *,
        limit: int = 8,
    ) -> List[Dict[str, str]]:
        """Return prior completed user/assistant turns in chronological order."""
        records = self.list_runs(
            project_id=project_id,
            limit=limit,
            session_id=session_id,
        )
        return [
            {
                "query": str(record.get("query") or ""),
                "answer": str(record.get("answer") or ""),
            }
            for record in reversed(records)
            if record.get("query") and record.get("answer")
        ]

    def list_runs(
        self,
        project_id: Optional[str] = None,
        limit: int = 50,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.persist:
            return []
        folder = self.data_dir / "runs"
        if not folder.exists():
            return []
        records: List[Dict[str, Any]] = []
        for path in folder.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if project_id and payload.get("project_id") != project_id:
                continue
            if session_id and payload.get("session_id") != session_id:
                continue
            records.append(payload)
        records.sort(key=lambda item: float(item.get("started_at") or 0), reverse=True)
        return records[:limit]


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]
