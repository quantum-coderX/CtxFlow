"""Stored source documents and page-located text extraction / OCR.

Uploaded PDFs are kept as immutable, hashed project documents. Extraction
returns text per page with the method used, so every passage keeps a page
location. Engines:

* ``text_layer`` - PyMuPDF embedded text (no OCR).
* ``tesseract`` - Tesseract OCR through PyMuPDF for pages without text.
* ``nougat`` - Meta's Nougat CLI for scientific PDFs (math-aware); optional.

``auto`` uses the text layer and falls back to Tesseract for scanned pages
when it is installed. A requested engine that is not installed returns
``unavailable``; pages without recoverable text make the result ``partial``.
OCR output is extracted evidence, not ground truth.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, ValidationError

from researchos.analytics import _Strict, _validation_message
from researchos.project_store import ProjectStore, StoreError, safe_key, sha256_bytes, slugify

OCR_ENGINE_VERSION = "ocr-1.0"
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_PAGE_CHARS = 20_000
MIN_TEXT_CHARS = 25
NOUGAT_TIMEOUT_S = 900.0


class DocumentError(ValueError):
    """A document or extraction request is invalid."""


class OcrRequest(_Strict):
    engine: Literal["auto", "text_layer", "tesseract", "nougat"] = "auto"
    max_pages: int = Field(default=50, ge=1, le=500)
    language: str = Field(default="eng", pattern=r"^[a-z]{3}(\+[a-z]{3}){0,3}$")


def engine_availability() -> Dict[str, bool]:
    try:
        import fitz  # noqa: F401

        pymupdf = True
    except ImportError:
        pymupdf = False
    tesseract = bool(shutil.which("tesseract") or os.environ.get("TESSDATA_PREFIX"))
    return {
        "text_layer": pymupdf,
        "tesseract": pymupdf and tesseract,
        "nougat": bool(os.environ.get("RESEARCHOS_NOUGAT_BIN") or shutil.which("nougat")),
    }


def _nougat(pdf_path: Path, max_pages: int) -> Dict[str, Any]:
    binary = os.environ.get("RESEARCHOS_NOUGAT_BIN") or shutil.which("nougat")
    with tempfile.TemporaryDirectory(prefix="researchos-nougat-") as out:
        cmd = [binary, str(pdf_path), "-o", out, "--pages", f"1-{max_pages}"]
        try:
            completed = subprocess.run(cmd, capture_output=True, timeout=NOUGAT_TIMEOUT_S,
                                       stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": f"Nougat exceeded {NOUGAT_TIMEOUT_S:.0f}s."}
        except OSError as exc:
            return {"status": "error", "error": f"Could not start Nougat: {exc}"}
        outputs = sorted(Path(out).glob("*.mmd"))
        if completed.returncode != 0 or not outputs:
            return {"status": "error",
                    "error": f"Nougat failed ({completed.returncode}): "
                             f"{completed.stderr.decode('utf-8', 'replace')[-1000:]}"}
        return {"status": "ok", "text": outputs[0].read_text("utf-8", "replace")}


def extract_pages(pdf_path: Path, request: OcrRequest) -> Dict[str, Any]:
    available = engine_availability()
    base: Dict[str, Any] = {"engine": request.engine, "engine_version": OCR_ENGINE_VERSION, "availability": available}
    if request.engine != "auto" and not available.get(request.engine, False):
        return {**base, "status": "unavailable", "pages": [], "warnings": [],
                "error": f"OCR engine {request.engine!r} is not installed."}
    if not available["text_layer"]:
        return {**base, "status": "unavailable", "pages": [], "warnings": [],
                "error": "PyMuPDF is required to read PDFs (pip install pymupdf)."}
    import fitz

    warnings: List[str] = []
    try:
        document = fitz.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - parser failure must be visible
        return {**base, "status": "error", "pages": [], "warnings": [], "error": f"Could not open PDF: {exc}"}
    try:
        if document.needs_pass:
            return {**base, "status": "error", "pages": [], "warnings": [], "error": "PDF is encrypted."}
        page_count = document.page_count
        limit = min(page_count, request.max_pages)
        if limit < page_count:
            warnings.append(f"Only the first {limit} of {page_count} pages were processed.")
        if request.engine == "nougat":
            result = _nougat(pdf_path, limit)
            if result["status"] != "ok":
                return {**base, "status": "error", "pages": [], "warnings": warnings,
                        "page_count": page_count, "error": result["error"]}
            text = result["text"]
            warnings.append("Nougat output is document-level markdown; page locations are not preserved.")
            return {**base, "status": "partial", "page_count": page_count, "warnings": warnings,
                    "pages": [{"page": None, "method": "nougat", "chars": len(text),
                               "text": text[: MAX_PAGE_CHARS * 10], "truncated": len(text) > MAX_PAGE_CHARS * 10}]}
        pages: List[Dict[str, Any]] = []
        for index in range(limit):
            page = document.load_page(index)
            text = ""
            method = "text_layer"
            if request.engine in ("auto", "text_layer"):
                text = page.get_text("text") or ""
            use_ocr = request.engine == "tesseract" or (
                request.engine == "auto" and len(text.strip()) < MIN_TEXT_CHARS and available["tesseract"]
            )
            if use_ocr:
                try:
                    textpage = page.get_textpage_ocr(language=request.language, dpi=300, full=True)
                    text = page.get_text("text", textpage=textpage) or ""
                    method = "tesseract"
                except Exception as exc:  # noqa: BLE001 - per-page OCR failure is reported
                    warnings.append(f"Page {index + 1}: OCR failed ({type(exc).__name__}: {exc}).")
                    method = "ocr_failed"
            stripped = text.strip()
            if len(stripped) < MIN_TEXT_CHARS and method != "ocr_failed":
                method = "no_text"
                warnings.append(
                    f"Page {index + 1} has no extractable text"
                    + ("" if available["tesseract"] else " (scanned? Tesseract is not installed)") + "."
                )
            pages.append({
                "page": index + 1,
                "method": method,
                "chars": len(stripped),
                "text": stripped[:MAX_PAGE_CHARS],
                "truncated": len(stripped) > MAX_PAGE_CHARS,
            })
    finally:
        document.close()
    good = [p for p in pages if p["method"] in ("text_layer", "tesseract")]
    if not good:
        status = "error"
    elif len(good) < len(pages) or limit < page_count:
        status = "partial"
    else:
        status = "ok"
    return {**base, "status": status, "page_count": page_count, "pages": pages, "warnings": warnings,
            "error": None if good else "No page produced text."}


class DocumentStore:
    """Immutable stored documents (PDFs) per project, with extraction results."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def _dir(self, project_id: str, document_id: Optional[str] = None) -> Path:
        if document_id is None:
            return self.store.kind_dir(project_id, "documents")
        try:
            return self.store.kind_dir(project_id, "documents", safe_key(document_id, label="document_id"))
        except StoreError as exc:
            raise DocumentError(str(exc)) from None

    def add(self, project_id: str, *, filename: str, payload: bytes, source: str = "upload",
            source_node_id: Optional[str] = None) -> Dict[str, Any]:
        if not payload:
            raise DocumentError("Document is empty.")
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise DocumentError(f"Document exceeds {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.")
        if not payload.startswith(b"%PDF-"):
            raise DocumentError("Only PDF documents are supported.")
        digest = sha256_bytes(payload)
        document_id = f"pdf-{slugify(Path(filename or 'document').stem, max_len=40)}-{digest[:10]}"
        directory = self._dir(project_id, document_id)
        with self.store.lock:
            existing = self.store.read_json(directory / "document.json")
            if isinstance(existing, dict):
                return {**existing, "reused": True}
            self.store.write_bytes(directory / "original.pdf", payload)
            record = {
                "document_id": document_id,
                "project_id": project_id,
                "filename": Path(filename or "").name,
                "sha256": digest,
                "size_bytes": len(payload),
                "source": source,
                "source_node_id": source_node_id,
                "added_at": time.time(),
                "extractions": [],
            }
            self.store.write_json(directory / "document.json", record)
            return {**record, "reused": False}

    def get(self, project_id: str, document_id: str) -> Dict[str, Any]:
        record = self.store.read_json(self._dir(project_id, document_id) / "document.json")
        if not isinstance(record, dict):
            raise DocumentError(f"Document {document_id!r} not found.")
        return record

    def list(self, project_id: str) -> List[Dict[str, Any]]:
        records = []
        for directory in self.store.list_subdirs(self._dir(project_id)):
            record = self.store.read_json(directory / "document.json")
            if isinstance(record, dict):
                records.append(record)
        records.sort(key=lambda r: r.get("added_at", 0), reverse=True)
        return records

    def extract(self, project_id: str, document_id: str, request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            parsed = OcrRequest.model_validate(request or {})
        except ValidationError as exc:
            raise DocumentError(f"Invalid OCR request: {_validation_message(exc)}") from None
        record = self.get(project_id, document_id)
        directory = self._dir(project_id, document_id)
        started = time.monotonic()
        result = extract_pages(directory / "original.pdf", parsed)
        result.update({
            "document_id": document_id,
            "document_sha256": record["sha256"],
            "request": parsed.model_dump(),
            "duration_ms": (time.monotonic() - started) * 1000,
            "extracted_at": time.time(),
        })
        with self.store.lock:
            number = len(record.get("extractions", [])) + 1
            self.store.write_json(directory / f"extraction-{number}.json", result)
            record.setdefault("extractions", []).append({
                "number": number, "engine": parsed.engine, "status": result["status"],
                "extracted_at": result["extracted_at"],
            })
            self.store.write_json(directory / "document.json", record)
        return {**result, "extraction": number}

    def extraction(self, project_id: str, document_id: str, number: Optional[int] = None) -> Dict[str, Any]:
        record = self.get(project_id, document_id)
        extractions = record.get("extractions") or []
        if not extractions:
            raise DocumentError(f"Document {document_id!r} has not been extracted.")
        chosen = int(number or extractions[-1]["number"])
        payload = self.store.read_json(self._dir(project_id, document_id) / f"extraction-{chosen}.json")
        if not isinstance(payload, dict):
            raise DocumentError(f"Extraction {chosen} not found.")
        return payload
