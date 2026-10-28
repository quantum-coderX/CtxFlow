"""Curated, offline demo corpus for the ResearchOS workspace.

The corpus contains only real arXiv papers (titles, authors, abstracts, IDs) so
a demo can populate the graph without depending on a live search provider. The
two curated relations in ``relations.json`` connect claims from real papers; the
demo shows correction propagation and contested claims without pretending a
model detected them.

Seed uses the same ingest path as ``POST /api/papers/ingest``: one paper node
plus one claim node per abstract sentence, linked with an ``extracted_from``
edge. Curated relations from ``relations.json`` are applied after ingestion,
producing real ``supersedes``/``contradicts`` edges and status transitions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ctxflow import CtxFlow
from ctxflow.models import Edge, Node, NodeStatus

_HERE = Path(__file__).parent
_CORPUS_PATH = _HERE / "corpus.json"
_RELATIONS_PATH = _HERE / "relations.json"
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_MAX_CLAIMS_PER_PAPER = 12


def load_corpus(path: Path | None = None) -> List[Dict[str, Any]]:
    """Load the checked-in demo papers."""
    corpus_path = path or _CORPUS_PATH
    if not corpus_path.exists():
        raise FileNotFoundError(f"demo corpus does not exist: {corpus_path}")
    with corpus_path.open("r", encoding="utf-8") as handle:
        papers = json.load(handle)
    if not isinstance(papers, list) or not papers:
        raise ValueError("demo corpus must be a non-empty JSON array")
    return papers


def load_relations(path: Path | None = None) -> List[Dict[str, Any]]:
    """Load the curated demo relations (supersedes / contradicts)."""
    relations_path = path or _RELATIONS_PATH
    if not relations_path.exists():
        return []
    with relations_path.open("r", encoding="utf-8") as handle:
        relations = json.load(handle)
    if not isinstance(relations, list):
        raise ValueError("demo relations must be a JSON array")
    return relations


def _paper_keys(node: Node) -> set[str]:
    metadata = node.metadata or {}
    keys: set[str] = set()
    for field in ("arxiv_id", "demo_id"):
        value = metadata.get(field)
        if value:
            keys.add(str(value).strip().lower())
    title = str(metadata.get("title") or "").strip().lower()
    if title:
        keys.add(f"title:{title}")
    return keys


def _existing_paper_keys(ctxflow: CtxFlow) -> set[str]:
    keys: set[str] = set()
    for node in ctxflow.graph.all_nodes():
        if node.node_type == "paper":
            keys |= _paper_keys(node)
    return keys


def _record_key(record: Dict[str, Any]) -> str:
    for field in ("arxiv_id", "demo_id"):
        value = str(record.get(field) or "").strip().lower()
        if value:
            return value
    return f"title:{str(record.get('title') or '').strip().lower()}"


def _claim_sentences(abstract: str) -> List[str]:
    sentences = []
    for sentence in _SENTENCE_RE.split(abstract or ""):
        cleaned = " ".join(sentence.split())
        if len(cleaned) >= 20:
            sentences.append(cleaned)
        if len(sentences) >= _MAX_CLAIMS_PER_PAPER:
            break
    return sentences


def _find_paper(ctxflow: CtxFlow, selector: Dict[str, Any]) -> Optional[Node]:
    demo_id = str(selector.get("demo_id") or "").strip().lower()
    arxiv_id = str(selector.get("arxiv_id") or "").strip().lower()
    for node in ctxflow.graph.all_nodes():
        if node.node_type != "paper":
            continue
        metadata = node.metadata or {}
        if demo_id and str(metadata.get("demo_id") or "").strip().lower() == demo_id:
            return node
        if arxiv_id and str(metadata.get("arxiv_id") or "").strip().lower() == arxiv_id:
            return node
    return None


def _find_claim(
    ctxflow: CtxFlow, paper_id: str, contains: str
) -> Optional[Node]:
    needle = " ".join((contains or "").split()).lower()
    if not needle:
        return None
    for node in ctxflow.graph.all_nodes():
        if node.node_type != "claim":
            continue
        metadata = node.metadata or {}
        if metadata.get("source_paper_node_id") != paper_id:
            continue
        if needle in " ".join(node.content.split()).lower():
            return node
    return None


def apply_curated_relations(
    ctxflow: CtxFlow, relations: List[Dict[str, Any]] | None = None
) -> Dict[str, Any]:
    """Apply curated supersedes/contradicts edges to an ingested corpus.

    Idempotent: an existing edge between the same pair and relation is not
    duplicated, and status transitions are only counted the first time.
    """
    relations = relations if relations is not None else load_relations()
    applied = 0
    already = 0
    skipped = 0
    applied_relations: List[Dict[str, Any]] = []

    for item in relations:
        old_paper = _find_paper(ctxflow, item.get("old") or {})
        new_paper = _find_paper(ctxflow, item.get("new") or {})
        if old_paper is None or new_paper is None:
            skipped += 1
            continue

        old_claim = _find_claim(
            ctxflow, old_paper.id, str((item.get("old") or {}).get("claim_contains") or "")
        )
        new_claim = _find_claim(
            ctxflow, new_paper.id, str((item.get("new") or {}).get("claim_contains") or "")
        )
        if old_claim is None or new_claim is None:
            skipped += 1
            continue

        relation = str(item.get("relation") or "supersedes")
        if relation not in {"supersedes", "contradicts"}:
            skipped += 1
            continue

        existing = [
            edge
            for edge in ctxflow.graph.get_edges_from(new_claim.id)
            if edge.target_id == old_claim.id and edge.relation_type == relation
        ]
        if existing:
            already += 1
            continue

        confidence = float(item.get("confidence") or 0.0)
        ctxflow.graph.add_edge(
            Edge(
                source_id=new_claim.id,
                target_id=old_claim.id,
                relation_type=relation,
                weight=confidence,
                metadata={
                    "confidence": confidence,
                    "evidence": str(item.get("rationale") or ""),
                    "scenario": str(item.get("scenario") or ""),
                    "curated": True,
                },
            )
        )
        if relation == "supersedes":
            old_claim.status = NodeStatus.SUPERSEDED
            old_claim.superseded_by = new_claim.id
        else:
            old_claim.status = NodeStatus.CONTESTED

        applied += 1
        applied_relations.append(
            {
                "scenario": str(item.get("scenario") or ""),
                "relation": relation,
                "confidence": confidence,
                "old_claim_id": old_claim.id,
                "new_claim_id": new_claim.id,
            }
        )

    return {
        "relations_applied": applied,
        "relations_already_present": already,
        "relations_skipped": skipped,
        "applied_relations": applied_relations,
    }


def seed_demo_corpus(
    ctxflow: CtxFlow,
    *,
    project_id: str = "default",
    papers: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Ingest the demo corpus into ``ctxflow`` (idempotent per arXiv/demo ID)."""
    records = papers if papers is not None else load_corpus()
    existing = _existing_paper_keys(ctxflow)

    papers_ingested = 0
    claims_ingested = 0
    skipped = 0
    for record in records:
        key = _record_key(record)
        title = str(record.get("title") or "").strip()
        if not title:
            skipped += 1
            continue
        if key in existing:
            skipped += 1
            continue

        arxiv_id = str(record.get("arxiv_id") or "").strip()
        authors = list(record.get("authors") or [])
        year = record.get("year")
        url = str(record.get("url") or "")
        metadata = {
            "project_id": project_id,
            "title": title,
            "authors": authors,
            "year": year,
            "arxiv_id": arxiv_id,
            "url": url,
            "demo_corpus": True,
        }
        demo_id = str(record.get("demo_id") or "").strip()
        if demo_id:
            metadata["demo_id"] = demo_id
        if record.get("synthetic"):
            metadata["synthetic"] = True
            metadata["scenario"] = str(record.get("scenario") or "")

        paper_content = f"Paper: {title}"
        if authors:
            paper_content += f"\nAuthors: {', '.join(authors[:5])}"
        if year:
            paper_content += f"\nYear: {year}"
        if arxiv_id:
            paper_content += f"\narXiv: {arxiv_id}"

        tags = ctxflow.extractor.extract_tags(f"{title} {record.get('abstract', '')}")
        paper_id = ctxflow.ingest(
            content=paper_content,
            node_type="paper",
            tags=tags,
            metadata=metadata,
            check_contradictions=False,
        )
        papers_ingested += 1

        for sentence in _claim_sentences(str(record.get("abstract") or "")):
            ctxflow.ingest(
                content=sentence,
                node_type="claim",
                source_id=paper_id,
                relation_type="extracted_from",
                tags=tags,
                metadata={
                    **metadata,
                    "source_paper": title,
                    "source_paper_node_id": paper_id,
                },
                check_contradictions=False,
            )
            claims_ingested += 1

        existing.add(key)

    relation_stats = apply_curated_relations(ctxflow)

    return {
        "papers_ingested": papers_ingested,
        "claims_ingested": claims_ingested,
        "papers_skipped": skipped,
        "project_id": project_id,
        "nodes": ctxflow.graph.node_count,
        "edges": ctxflow.graph.edge_count,
        "superseded_claims": sum(
            1
            for node in ctxflow.graph.all_nodes()
            if node.status == NodeStatus.SUPERSEDED
        ),
        "contested_claims": sum(
            1
            for node in ctxflow.graph.all_nodes()
            if node.status == NodeStatus.CONTESTED
        ),
        **relation_stats,
    }
