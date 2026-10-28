"""Paper Reader Skill — PDF extraction and structured claim mapping.

Parses PDF documents (via PyMuPDF / ``fitz``) and distils them into
structured CtxFlow nodes: claims, methods, results, and findings.
Each extracted item gets its own tags, confidence score, and provenance
link back to the source paper node.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PaperSection:
    """A section extracted from a PDF.

    Attributes
    ----------
    title : str
        Section heading (e.g. "Abstract", "Results").
    text : str
        Full text content of the section.
    page_numbers : list[int]
        Pages this section spans.
    """

    title: str = ""
    text: str = ""
    page_numbers: List[int] = field(default_factory=list)


@dataclass
class ExtractedClaim:
    """A structured claim extracted from a paper.

    Attributes
    ----------
    claim_type : str
        One of ``"claim"``, ``"method"``, ``"result"``, ``"finding"``,
        ``"hypothesis"``.
    text : str
        The extracted statement (one sentence).
    confidence : float
        How clearly this claim is stated in the source (0–1).
    tags : list[str]
        Keywords associated with this specific claim.
    source_section : str
        Which section the claim was extracted from.
    page_number : int | None
        Page number where this claim appears.
    """

    claim_type: str = "claim"
    text: str = ""
    confidence: float = 0.5
    tags: List[str] = field(default_factory=list)
    source_section: str = ""
    page_number: Optional[int] = None


# ---------------------------------------------------------------------------
# PDF Extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract all text from a PDF file.

    Parameters
    ----------
    pdf_path : str
        Path to the PDF file.

    Returns
    -------
    str
        Concatenated text from all pages.

    Raises
    ------
    ImportError
        If ``pymupdf`` is not installed.
    FileNotFoundError
        If *pdf_path* does not exist.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF is required for PDF parsing. "
            "Install it with: pip install ctxflow[researchos]"
        ) from exc

    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(path))
    pages_text: List[str] = []
    for page in doc:
        pages_text.append(page.get_text())
    doc.close()

    return "\n\n".join(pages_text)


def extract_sections_from_pdf(pdf_path: str) -> List[PaperSection]:
    """Extract structured sections from a PDF.

    Uses font-size heuristics to detect section headings. Falls back
    to page-level chunking if heading detection fails.

    Parameters
    ----------
    pdf_path : str
        Path to the PDF file.

    Returns
    -------
    list[PaperSection]
    """
    try:
        import fitz
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF is required for PDF parsing. "
            "Install it with: pip install ctxflow[researchos]"
        ) from exc

    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(path))
    sections: List[PaperSection] = []
    current_section = PaperSection(title="Preamble", page_numbers=[])
    current_text_parts: List[str] = []

    for page_num, page in enumerate(doc):
        blocks = page.get_text("dict", flags=11).get("blocks", [])
        for block in blocks:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                line_text = ""
                max_font_size = 0.0
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                    font_size = span.get("size", 0.0)
                    if font_size > max_font_size:
                        max_font_size = font_size

                line_text = line_text.strip()
                if not line_text:
                    continue

                # Heuristic: lines with font size > 12 and short length
                # are likely section headings
                is_heading = (
                    max_font_size >= 12.0
                    and len(line_text) < 100
                    and not line_text.endswith(".")
                )

                if is_heading and current_text_parts:
                    # Save current section
                    current_section.text = "\n".join(current_text_parts)
                    if current_section.text.strip():
                        sections.append(current_section)
                    # Start new section
                    current_section = PaperSection(
                        title=line_text, page_numbers=[page_num]
                    )
                    current_text_parts = []
                elif is_heading:
                    current_section = PaperSection(
                        title=line_text, page_numbers=[page_num]
                    )
                    current_text_parts = []
                else:
                    current_text_parts.append(line_text)
                    if page_num not in current_section.page_numbers:
                        current_section.page_numbers.append(page_num)

    # Save last section
    if current_text_parts:
        current_section.text = "\n".join(current_text_parts)
        if current_section.text.strip():
            sections.append(current_section)

    page_count = doc.page_count
    doc.close()

    # Fallback: if no sections were detected, return full text as one section
    if not sections:
        full_text = extract_text_from_pdf(pdf_path)
        sections = [PaperSection(
            title="Full Document",
            text=full_text,
            page_numbers=list(range(page_count)),
        )]

    return sections


# ---------------------------------------------------------------------------
# Claim Distillation
# ---------------------------------------------------------------------------

# Type alias for LLM callable
LLMCallable = Callable[[str, str], str]


class PaperReaderSkill:
    """Reads PDFs and distils them into structured claims for CtxFlow.

    Parameters
    ----------
    llm : callable | None
        An LLM callable ``(prompt, system) -> str`` for claim extraction.
        If ``None``, falls back to heuristic chunking.
    chunk_size : int
        Maximum characters per chunk when splitting text for LLM processing.
    """

    def __init__(
        self,
        llm: Optional[LLMCallable] = None,
        chunk_size: int = 3000,
    ) -> None:
        self._llm = llm
        self.chunk_size = chunk_size

    def read_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """Extract full paper metadata and text from a PDF.

        Returns
        -------
        dict
            Keys: ``title``, ``sections``, ``full_text``, ``page_count``.
        """
        sections = extract_sections_from_pdf(pdf_path)
        full_text = extract_text_from_pdf(pdf_path)

        page_count = len(sections)
        try:
            import fitz

            with fitz.open(str(pdf_path)) as document:
                page_count = document.page_count
        except Exception:  # pragma: no cover - best-effort metadata
            pass

        # Attempt to extract title from first section or first line
        title = ""
        if sections and sections[0].title != "Preamble":
            title = sections[0].title
        elif full_text:
            first_line = full_text.split("\n")[0].strip()
            if len(first_line) < 200:
                title = first_line

        return {
            "title": title,
            "sections": sections,
            "full_text": full_text,
            "page_count": page_count,
        }

    def read_selected_sections(
        self,
        pdf_path: str,
        *,
        max_chars_per_section: int = 2500,
        max_claims_per_section: int = 3,
    ) -> Dict[str, Any]:
        """Return bounded introduction, results, and conclusion excerpts."""
        import re

        wanted = {
            "introduction": re.compile(r"\b(introduction|intro)\b", re.IGNORECASE),
            "results": re.compile(r"\bresults?\b", re.IGNORECASE),
            "conclusion": re.compile(r"\bconclusions?\b", re.IGNORECASE),
        }
        selected: Dict[str, PaperSection] = {}
        for section in extract_sections_from_pdf(pdf_path):
            title = re.sub(r"^\s*\d+(?:\.\d+)*[\s.:)]*", "", section.title)
            for key, pattern in wanted.items():
                if key not in selected and pattern.search(title):
                    selected[key] = section
                    break

        content_parts: List[str] = []
        claims: List[Dict[str, Any]] = []
        for key in ("introduction", "results", "conclusion"):
            section = selected.get(key)
            if section is None:
                continue
            text = section.text.strip()[:max_chars_per_section]
            if not text:
                continue
            content_parts.append(f"{section.title}\n{text}")
            bounded_section = PaperSection(
                title=section.title,
                text=text,
                page_numbers=list(section.page_numbers),
            )
            for claim in self._extract_claims_heuristic(bounded_section)[
                :max_claims_per_section
            ]:
                claims.append({
                    "type": claim.claim_type,
                    "text": claim.text,
                    "confidence": claim.confidence,
                    "tags": claim.tags,
                    "section": claim.source_section,
                    "page": claim.page_number,
                    "extraction_method": "heuristic_pdf_section",
                    "evidence_kind": "full_text_excerpt",
                })

        return {"content": "\n\n".join(content_parts), "claims": claims}

    def extract_claims(self, pdf_path: str) -> List[ExtractedClaim]:
        """Extract structured claims from a PDF.

        Processes each section through the LLM claim extractor (if available)
        or falls back to sentence-level chunking.

        Parameters
        ----------
        pdf_path : str
            Path to the PDF file.

        Returns
        -------
        list[ExtractedClaim]
        """
        sections = extract_sections_from_pdf(pdf_path)
        all_claims: List[ExtractedClaim] = []

        for section in sections:
            if not section.text.strip():
                continue

            if self._llm is not None:
                claims = self._extract_claims_with_llm(section)
            else:
                claims = self._extract_claims_heuristic(section)

            all_claims.extend(claims)

        return all_claims

    def _extract_claims_with_llm(
        self, section: PaperSection
    ) -> List[ExtractedClaim]:
        """Use LLM to extract structured claims from a section."""
        from ctxflow.llm_extractor import LLMExtractor

        assert self._llm is not None
        extractor = LLMExtractor(llm=self._llm)

        # Chunk the section text if too long
        chunks = self._chunk_text(section.text)
        claims: List[ExtractedClaim] = []

        for chunk in chunks:
            try:
                raw_claims = extractor.extract_claims(chunk)
                for rc in raw_claims:
                    claims.append(ExtractedClaim(
                        claim_type=rc.get("type", "claim"),
                        text=rc.get("text", ""),
                        confidence=rc.get("confidence", 0.5),
                        tags=rc.get("tags", []),
                        source_section=section.title,
                        page_number=section.page_numbers[0] if section.page_numbers else None,
                    ))
            except Exception:
                logger.warning(
                    "LLM claim extraction failed for section '%s'",
                    section.title, exc_info=True,
                )
                claims.extend(self._extract_claims_heuristic(section))

        return claims

    def _extract_claims_heuristic(
        self, section: PaperSection
    ) -> List[ExtractedClaim]:
        """Fallback: split text into sentences and treat each as a claim."""
        import re
        sentences = re.split(r'(?<=[.!?])\s+', section.text)
        claims: List[ExtractedClaim] = []

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 20:
                continue

            # Simple keyword-based type detection
            lower = sentence.lower()
            if any(kw in lower for kw in ("result", "accuracy", "performance", "achieves", "outperforms", "%")):
                claim_type = "result"
            elif any(kw in lower for kw in ("we propose", "we present", "method", "approach", "algorithm")):
                claim_type = "method"
            elif any(kw in lower for kw in ("finding", "found", "observe", "discover")):
                claim_type = "finding"
            elif any(kw in lower for kw in ("hypothes", "conjecture", "expect")):
                claim_type = "hypothesis"
            else:
                claim_type = "claim"

            # Extract simple keyword tags
            words = re.findall(r'\b[a-zA-Z]{4,}\b', lower)
            stop = {"this", "that", "with", "from", "have", "been", "also", "which", "their", "more", "than"}
            tags = [w for w in words if w not in stop][:5]

            claims.append(ExtractedClaim(
                claim_type=claim_type,
                text=sentence[:500],
                confidence=0.5,
                tags=tags,
                source_section=section.title,
                page_number=section.page_numbers[0] if section.page_numbers else None,
            ))

        return claims

    def _chunk_text(self, text: str) -> List[str]:
        """Split text into chunks of approximately ``chunk_size`` characters."""
        if len(text) <= self.chunk_size:
            return [text]

        chunks: List[str] = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            # Try to break at a sentence boundary
            if end < len(text):
                last_period = text.rfind(".", start, end)
                if last_period > start + self.chunk_size // 2:
                    end = last_period + 1
            chunks.append(text[start:end].strip())
            start = end

        return [c for c in chunks if c]
