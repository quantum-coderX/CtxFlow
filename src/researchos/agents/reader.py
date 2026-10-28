"""Reader Agent — ingests papers and distils them into CtxFlow nodes.

The Reader takes paper results from the Retriever, downloads/parses PDFs
(when available), extracts structured claims using the PaperReaderSkill,
and ingests them into the CtxFlow knowledge graph with contradiction
detection.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ctxflow import CtxFlow
from researchos.agents.base import AgentResult, BaseAgent
from researchos.skills.paper_reader import PaperReaderSkill
from researchos.skills.source_fetch import canonical_url

logger = logging.getLogger(__name__)

_FALSEY = frozenset({"0", "false", "no", "off"})
_ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?")
_FULLTEXT_MAX_PAPERS = 3
_FULLTEXT_PHASE_BUDGET_S = 8.0
_FULLTEXT_DOWNLOAD_TIMEOUT_S = 5.0
_FULLTEXT_MAX_PDF_BYTES = 12 * 1024 * 1024


def llm_claim_extraction_enabled() -> bool:
    """Whether abstracts may be sent to the model for claim extraction.

    Free provider tiers cap tokens per minute (Groq's is 8k), and reading a
    handful of fresh abstracts can consume the whole window before
    synthesis runs. Set ``RESEARCHOS_LLM_CLAIM_EXTRACTION=0`` to keep claim
    extraction deterministic and reserve the model for the answer.
    """

    raw = str(os.environ.get("RESEARCHOS_LLM_CLAIM_EXTRACTION", "")).strip().lower()
    return raw not in _FALSEY


def _fulltext_top3_enabled() -> bool:
    """Enable bounded full-text reading only for the explicit ``top3`` value."""
    return str(os.environ.get("RESEARCHOS_FULLTEXT", "off")).strip().lower() == "top3"


class ReaderAgent(BaseAgent):
    """Agent that reads papers and ingests claims into CtxFlow.

    Parameters
    ----------
    ctxflow : CtxFlow
        The CtxFlow instance for knowledge graph ingestion.
    llm : callable | None
        LLM callable for claim extraction.
    """

    def __init__(
        self,
        ctxflow: CtxFlow,
        llm: Optional[Callable[[str, str], str]] = None,
        paper_reader: Optional[PaperReaderSkill] = None,
    ) -> None:
        super().__init__(name="reader", llm=llm)
        self._ctx = ctxflow
        self._paper_reader = paper_reader or PaperReaderSkill(llm=llm)

    def execute(self, **kwargs: Any) -> AgentResult:
        """Read papers and ingest their claims into the knowledge graph.

        Expects ``previous_results`` from the Retriever containing
        ``external_results`` with paper abstracts.

        Parameters
        ----------
        query : str
            The original query.
        context : dict
            Context from the pipeline.
        previous_results : list[AgentResult]
            Previous agent results (expects Retriever output).

        Returns
        -------
        AgentResult
            Contains list of ingested node IDs and paper metadata.
        """
        query: str = kwargs.get("query", "")
        context: Dict[str, Any] = kwargs.get("context", {})
        previous_results: List[AgentResult] = kwargs.get("previous_results", [])

        # Extract external results from retriever output
        papers_data: List[Dict[str, Any]] = []
        for prev in previous_results:
            if prev.agent_name == "retriever" and prev.data:
                papers_data = prev.data.get("external_results", [])
                break

        # Also check context for previous output
        if not papers_data and isinstance(context.get("previous_output"), dict):
            papers_data = context["previous_output"].get("external_results", [])

        if _fulltext_top3_enabled() and papers_data:
            papers_data = self._enrich_top_papers_with_fulltext(
                papers_data,
                context.get("fulltext_deadline"),
            )

        ingested_nodes: List[Dict[str, Any]] = []

        for paper in papers_data:
            try:
                node_ids, reused = self._ingest_paper_detail(paper, query)
                ingested_nodes.append({
                    "title": paper.get("title", ""),
                    "node_ids": node_ids,
                    "source": paper.get("source", ""),
                    "source_id": paper.get("source_id", ""),
                    "source_node_id": node_ids[0] if node_ids else "",
                    "access_status": paper.get("access_status", ""),
                    "url": paper.get("url", ""),
                    "reused": reused,
                })
            except Exception as exc:
                logger.warning(
                    "Failed to ingest paper '%s': %s",
                    paper.get("title", "unknown"), exc,
                )

        return AgentResult(
            agent_name=self.name,
            success=True,
            data={
                "ingested_papers": ingested_nodes,
                "total_nodes_created": sum(
                    len(p["node_ids"]) for p in ingested_nodes if not p["reused"]
                ),
            },
            metadata={
                "papers_processed": len(papers_data),
                "papers_ingested": len(ingested_nodes),
            },
        )

    def _enrich_top_papers_with_fulltext(
        self,
        papers: List[Dict[str, Any]],
        deadline: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Enrich at most three arXiv results before claim ingestion."""
        stop_at = time.monotonic() + _FULLTEXT_PHASE_BUDGET_S
        if isinstance(deadline, (int, float)):
            stop_at = min(stop_at, float(deadline))

        enriched = [dict(paper) for paper in papers]
        for paper in enriched[:_FULLTEXT_MAX_PAPERS]:
            if paper.get("content") or paper.get("claims"):
                continue
            arxiv_id = str(paper.get("arxiv_id") or "").strip()
            if not _ARXIV_ID.fullmatch(arxiv_id):
                continue
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                logger.info("Skipping arXiv full-text reads: wall-clock budget exhausted")
                break

            try:
                pdf_bytes = self._download_arxiv_pdf(arxiv_id, remaining)
                with tempfile.TemporaryDirectory(prefix="researchos-arxiv-") as temp_dir:
                    pdf_path = Path(temp_dir) / "paper.pdf"
                    pdf_path.write_bytes(pdf_bytes)
                    excerpts = self._paper_reader.read_selected_sections(str(pdf_path))
                if excerpts.get("content") and excerpts.get("claims"):
                    paper.update(excerpts)
                    paper["access_status"] = "full_text"
            except Exception as exc:
                logger.warning(
                    "Full-text read failed for arXiv paper %s: %s",
                    arxiv_id,
                    exc,
                )
        return enriched

    @staticmethod
    def _download_arxiv_pdf(arxiv_id: str, remaining_s: float) -> bytes:
        """Fetch a size-limited PDF from arXiv within the remaining time."""
        from urllib.parse import quote

        url = f"https://arxiv.org/pdf/{quote(arxiv_id, safe='/.')}"
        timeout = min(_FULLTEXT_DOWNLOAD_TIMEOUT_S, remaining_s)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "ResearchOS/1.0 (bounded paper reader)"},
        )
        chunks: List[bytes] = []
        total_bytes = 0
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while True:
                if time.monotonic() - started >= remaining_s:
                    raise TimeoutError("arXiv PDF download exceeded the wall-clock budget")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _FULLTEXT_MAX_PDF_BYTES:
                    raise ValueError("arXiv PDF exceeds the 12 MiB size limit")
                chunks.append(chunk)
        if not chunks:
            raise ValueError("arXiv returned an empty PDF")
        return b"".join(chunks)

    def _ingest_paper(
        self, paper: Dict[str, Any], query: str
    ) -> List[str]:
        """Ingest a single paper and return its node IDs."""
        return self._ingest_paper_detail(paper, query)[0]

    def _ingest_paper_detail(
        self, paper: Dict[str, Any], query: str
    ) -> tuple[List[str], bool]:
        """Ingest a single paper's data into the knowledge graph.

        Creates a paper reference node and individual claim nodes
        linked to it. Returns ``(node_ids, reused)``; ``reused`` is true when
        the source already existed and nothing was created.
        """
        title = paper.get("title", "Unknown Paper")
        abstract = paper.get("abstract", "")
        authors = paper.get("authors", [])
        year = paper.get("year")
        doi = paper.get("doi", "")
        arxiv_id = paper.get("arxiv_id", "")

        # Prefer the Paper Reader skill when a local PDF is available.
        extracted_claims: List[Dict[str, Any]] = []
        pdf_path = paper.get("pdf_path")
        if pdf_path:
            try:
                info = self._paper_reader.read_pdf(str(pdf_path))
                if not paper.get("title") and info.get("title"):
                    title = info["title"]
                extracted_claims = [
                    {
                        "type": claim.claim_type,
                        "text": claim.text,
                        "confidence": claim.confidence,
                        "tags": claim.tags,
                    }
                    for claim in self._paper_reader.extract_claims(str(pdf_path))
                ]
            except Exception as exc:
                logger.warning(
                    "Paper Reader failed for %s (%s); falling back to abstract",
                    pdf_path,
                    exc,
                )

        # 1. Create paper reference node
        paper_content = f"Paper: {title}"
        if authors:
            paper_content += f"\nAuthors: {', '.join(authors[:5])}"
        if year:
            paper_content += f"\nYear: {year}"
        if doi:
            paper_content += f"\nDOI: {doi}"

        existing = self._find_duplicate(
            title,
            arxiv_id,
            doi,
            str(paper.get("source_id") or ""),
            str(paper.get("url") or ""),
            title_match=paper.get("source_kind") != "web",
        )
        if existing is not None:
            return [existing], True

        provenance = {
            "provider": paper.get("source", ""),
            "source_kind": paper.get("source_kind") or "academic",
            "retrieved_at": paper.get("retrieved_at", ""),
            "source_id": paper.get("source_id", ""),
        }
        paper_metadata = {
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "url": paper.get("url", ""),
            "source_url": paper.get("url", ""),
            **provenance,
        }

        paper_tags = set()
        # Extract tags from title
        for word in title.lower().split():
            cleaned = word.strip(".,;:!?()[]{}\"'")
            if len(cleaned) > 3:
                paper_tags.add(cleaned)

        # 2. Pick claims: PDF, pre-extracted source excerpts, full content, or abstract.
        extraction_default = "pdf_reader" if extracted_claims else ""
        if not extracted_claims and paper.get("claims"):
            extracted_claims = [dict(c) for c in paper["claims"] if isinstance(c, dict)]
        if not extracted_claims:
            source_text = paper.get("content") or abstract
            if source_text:
                extracted_claims = self._extract_abstract_claims(source_text)
        access_status = str(
            paper.get("access_status")
            or ("full_text" if pdf_path or paper.get("content") else "")
            or ("abstract_only" if abstract else "unavailable")
        )
        paper_metadata["access_status"] = access_status

        paper_node_id = self._ctx.ingest(
            content=paper_content,
            node_type="paper",
            tags=paper_tags,
            metadata=paper_metadata,
            check_contradictions=False,
        )

        node_ids = [paper_node_id]

        for claim in extracted_claims:
            claim_text = claim.get("text", "").strip()
            if not claim_text:
                continue
            claim_tags = set(claim.get("tags", []))
            claim_tags.update(paper_tags)

            claim_metadata: Dict[str, Any] = {
                "confidence": claim.get("confidence", 0.5),
                "source_paper": title,
                "source_doi": doi,
                "source_paper_node_id": paper_node_id,
                "source_url": paper.get("url", ""),
                "access_status": access_status,
                "extraction_method": claim.get("extraction_method") or extraction_default,
                **provenance,
            }
            for key in ("section", "page", "span_start", "span_end", "evidence_kind"):
                if claim.get(key) is not None:
                    claim_metadata[key] = claim[key]

            claim_node_id = self._ctx.ingest(
                content=claim_text[:500],
                node_type=claim.get("type", "claim"),
                source_id=paper_node_id,
                relation_type="extracted_from",
                tags=claim_tags,
                metadata=claim_metadata,
                check_contradictions=True,
            )
            node_ids.append(claim_node_id)

        return node_ids, False

    def _find_duplicate(
        self,
        title: str,
        arxiv_id: str,
        doi: str,
        source_id: str = "",
        url: str = "",
        title_match: bool = True,
    ) -> Optional[str]:
        title_key = " ".join(title.lower().split())
        url_key = canonical_url(url) if url else ""
        for node in self._ctx.graph.all_nodes():
            if node.node_type != "paper":
                continue
            meta = node.metadata or {}
            if source_id and str(meta.get("source_id") or "") == source_id:
                return node.id
            if arxiv_id and str(meta.get("arxiv_id") or "") == arxiv_id:
                return node.id
            if doi and str(meta.get("doi") or "") == doi:
                return node.id
            if url_key and canonical_url(str(meta.get("url") or "")) == url_key:
                return node.id
            existing_title = " ".join(str(meta.get("title") or "").lower().split())
            if title_key and title_match and existing_title == title_key:
                return node.id
        return None

    def _extract_abstract_claims(
        self, abstract: str
    ) -> List[Dict[str, Any]]:
        """Extract claims from an abstract using LLM or heuristics."""
        if self._llm and llm_claim_extraction_enabled():
            try:
                from ctxflow.llm_extractor import LLMExtractor
                extractor = LLMExtractor(llm=self._llm)
                claims = extractor.extract_claims(abstract)
                for claim in claims:
                    if isinstance(claim, dict):
                        claim.setdefault("extraction_method", "llm_abstract")
                        claim.setdefault("evidence_kind", "abstract")
                return claims
            except Exception as exc:
                logger.warning(
                    "LLM claim extraction failed (%s), using heuristic", exc
                )

        # Heuristic fallback: split abstract into sentences
        import re
        sentences = re.split(r'(?<=[.!?])\s+', abstract)
        claims: List[Dict[str, Any]] = []

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 20:
                continue

            words = re.findall(r'\b[a-zA-Z]{4,}\b', sentence.lower())
            stop = {"this", "that", "with", "from", "have", "been", "also", "which", "their"}
            tags = [w for w in words if w not in stop][:5]

            claims.append({
                "type": "claim",
                "text": sentence[:500],
                "confidence": 0.5,
                "tags": tags,
                "extraction_method": "heuristic_sentence",
                "evidence_kind": "abstract",
            })

        return claims
