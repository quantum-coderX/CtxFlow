"""Project-scoped file storage for workstation engines.

Datasets, analyses, charts, source records and generated artifacts live under
``<root>/projects/<project_id>/<kind>/``. Every path is derived from validated
identifiers; callers never supply raw filesystem paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from researchos.persistence import sanitize_project_id

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class StoreError(ValueError):
    """An identifier or stored record is invalid."""


def safe_key(value: str, *, label: str = "identifier") -> str:
    """Validate an identifier used as a path segment."""
    text = str(value or "").strip()
    if not _SAFE_KEY.fullmatch(text) or ".." in text:
        raise StoreError(
            f"Invalid {label} {value!r}: use 1-96 letters, digits, '.', '_' or '-'."
        )
    return text


def slugify(value: str, *, fallback: str = "item", max_len: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-").lower()
    slug = slug[:max_len].strip("-")
    return slug or fallback


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for content hashes and reproducible ids."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def stable_hash(value: Any, *, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


class ProjectStore:
    """Atomic JSON and blob storage rooted at one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def project_dir(self, project_id: str) -> Path:
        return self.root / "projects" / sanitize_project_id(project_id)

    def kind_dir(self, project_id: str, kind: str, *parts: str) -> Path:
        path = self.project_dir(project_id) / safe_key(kind, label="kind")
        for part in parts:
            path = path / safe_key(part)
        return path

    def write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def write_bytes(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    @staticmethod
    def read_json(path: Path) -> Optional[Any]:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def iter_json(self, directory: Path, pattern: str = "*.json") -> Iterator[Dict[str, Any]]:
        if not directory.exists():
            return
        for path in sorted(directory.glob(pattern)):
            if path.name.startswith(".tmp-"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload

    def list_subdirs(self, directory: Path) -> List[Path]:
        if not directory.exists():
            return []
        return sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))
