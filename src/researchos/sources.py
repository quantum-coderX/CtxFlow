"""Project-scoped source registry with stable identities and citation keys.

The CtxFlow project graph stays the authority for paper and claim nodes. This
registry records, per source, a stable identity (DOI > arXiv > URL > node id),
a content hash and version that change when the source record changes, the
access level actually held (metadata only / abstract / full text), and a
citation key that never changes once assigned.
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from researchos.project_store import ProjectStore, stable_hash

SOURCE_NODE_TYPES = frozenset({"paper", "source", "web_source"})
CLAIM_ORIGIN_DEFAULT = "author_reported"


class SourceError(ValueError):
    """A citation or source identifier cannot be resolved."""


def node_status(node: Any) -> str:
    status = getattr(node, "status", "active")
    return str(getattr(status, "value", status) or "active")


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")


def _authors(metadata: Dict[str, Any]) -> List[str]:
    raw = metadata.get("authors") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in re.split(r",| and ", raw) if part.strip()]
    return [str(author).strip() for author in raw if str(author).strip()][:50]


def _year(metadata: Dict[str, Any]) -> Optional[int]:
    value = metadata.get("year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 3000 else None


def _title(node: Any) -> str:
    metadata = node.metadata or {}
    title = str(metadata.get("title") or "").strip()
    if title:
        return title
    first = (node.content or "").strip().splitlines()[0:1]
    return re.sub(r"^Paper:\s*", "", first[0]).strip() if first else node.id


def _identity(node: Any) -> Dict[str, str]:
    metadata = node.metadata or {}
    doi = str(metadata.get("doi") or "").strip().lower()
    if doi:
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
        return {"scheme": "doi", "value": doi}
    arxiv = str(metadata.get("arxiv_id") or "").strip()
    if arxiv:
        return {"scheme": "arxiv", "value": re.sub(r"v\d+$", "", arxiv)}
    url = str(metadata.get("url") or "").strip()
    if url:
        return {"scheme": "url", "value": url}
    return {"scheme": "node", "value": node.id}


def _access_level(node: Any, claim_count: int) -> str:
    metadata = node.metadata or {}
    if metadata.get("page_count") or metadata.get("full_text_available"):
        return "full_text"
    if claim_count or len(node.content or "") > 400:
        return "abstract"
    return "metadata_only"


def _base_key(title: str, authors: List[str], year: Optional[int]) -> str:
    surname = ""
    if authors:
        first = _ascii(authors[0])
        parts = [p for p in re.split(r"[\s,]+", first) if p]
        if "," in first:
            surname = parts[0] if parts else ""
        else:
            surname = parts[-1] if parts else ""
    words = [w for w in re.findall(r"[A-Za-z]+", _ascii(title)) if w.lower() not in {"a", "an", "the", "on", "of"}]
    stem = re.sub(r"[^a-z]", "", surname.lower()) or "anon"
    return f"{stem}{year or 'nd'}{(words[0].lower() if words else 'source')[:20]}"


def claim_sources(graph: Any, claim_id: str) -> List[str]:
    """Return source-node ids a claim was extracted from."""
    sources = []
    for edge in graph.graph.get_edges_to(claim_id):
        source = graph.graph.get_node(edge.source_id)
        if source is not None and source.node_type in SOURCE_NODE_TYPES and edge.relation_type in (
            "extracted_from", "follows", "supports", "derived_from"
        ):
            sources.append(source.id)
    return sorted(set(sources))


class SourceRegistry:
    """Persisted, versioned source records derived from a project's graph."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def _path(self, project_id: str) -> Path:
        return self.store.project_dir(project_id) / "sources" / "registry.json"

    def _load(self, project_id: str) -> Dict[str, Any]:
        payload = self.store.read_json(self._path(project_id))
        if not isinstance(payload, dict):
            return {"sources": {}, "keys": {}}
        payload.setdefault("sources", {})
        payload.setdefault("keys", {})
        return payload

    def sync(self, project_id: str, graph: Any) -> List[Dict[str, Any]]:
        """Upsert one record per source node; content changes bump the version."""
        with self.store.lock:
            registry = self._load(project_id)
            sources: Dict[str, Any] = registry["sources"]
            keys: Dict[str, str] = registry["keys"]
            changed = False
            claim_counts: Dict[str, int] = {}
            for node in graph.graph.all_nodes():
                if node.node_type in SOURCE_NODE_TYPES:
                    continue
                for source_id in claim_sources(graph, node.id):
                    claim_counts[source_id] = claim_counts.get(source_id, 0) + 1
            nodes = sorted(
                (n for n in graph.graph.all_nodes() if n.node_type in SOURCE_NODE_TYPES),
                key=lambda n: (n.timestamp, n.id),
            )
            now = time.time()
            for node in nodes:
                metadata = node.metadata or {}
                node_project = str(metadata.get("project_id") or project_id)
                if node_project != project_id:
                    continue
                title = _title(node)
                authors = _authors(metadata)
                year = _year(metadata)
                fingerprint = {
                    "content": node.content,
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "doi": metadata.get("doi"),
                    "arxiv_id": metadata.get("arxiv_id"),
                    "url": metadata.get("url"),
                }
                content_hash = stable_hash(fingerprint, length=64)
                record = sources.get(node.id)
                if record is None:
                    key = _base_key(title, authors, year)
                    candidate, suffix = key, 0
                    while candidate in keys and keys[candidate] != node.id:
                        suffix += 1
                        candidate = f"{key}{chr(ord('a') + suffix - 1) if suffix <= 26 else suffix}"
                    keys[candidate] = node.id
                    record = {
                        "source_id": node.id,
                        "project_id": project_id,
                        "citation_key": candidate,
                        "version": 1,
                        "history": [{"version": 1, "content_hash": content_hash, "observed_at": now}],
                        "first_seen_at": now,
                    }
                    sources[node.id] = record
                    changed = True
                elif record.get("content_hash") != content_hash:
                    record["version"] = int(record.get("version", 1)) + 1
                    record.setdefault("history", []).append(
                        {"version": record["version"], "content_hash": content_hash, "observed_at": now}
                    )
                    changed = True
                updates = {
                    "content_hash": content_hash,
                    "identity": _identity(node),
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "doi": metadata.get("doi") or None,
                    "arxiv_id": metadata.get("arxiv_id") or None,
                    "url": metadata.get("url") or None,
                    "venue": metadata.get("venue") or metadata.get("journal") or None,
                    "node_type": node.node_type,
                    "status": node_status(node),
                    "retrieved_at": node.timestamp,
                    "access_level": _access_level(node, claim_counts.get(node.id, 0)),
                    "claim_count": claim_counts.get(node.id, 0),
                    "license_note": str(metadata.get("license") or "unknown"),
                    "provider": str(metadata.get("provider") or metadata.get("source") or "workspace"),
                }
                for field, value in updates.items():
                    if record.get(field) != value:
                        record[field] = value
                        changed = True
            if changed:
                self.store.write_json(self._path(project_id), registry)
            return sorted(
                (dict(r) for r in sources.values() if r.get("project_id") == project_id),
                key=lambda r: r["citation_key"],
            )

    def list(self, project_id: str) -> List[Dict[str, Any]]:
        registry = self._load(project_id)
        return sorted(registry["sources"].values(), key=lambda r: r["citation_key"])

    def get(self, project_id: str, source_id: str) -> Optional[Dict[str, Any]]:
        return self._load(project_id)["sources"].get(source_id)

    def resolve(self, project_id: str, graph: Any, ref_id: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Resolve a source or claim id to (source record, claim info or None)."""
        node = graph.graph.get_node(ref_id)
        if node is None:
            raise SourceError(f"Citation {ref_id!r} does not match any node in project {project_id!r}.")
        if str((node.metadata or {}).get("project_id") or project_id) != project_id:
            raise SourceError(f"Citation {ref_id!r} belongs to another project.")
        if node.node_type in SOURCE_NODE_TYPES:
            record = self.get(project_id, node.id)
            if record is None:
                raise SourceError(f"Source {ref_id!r} is not registered; sync the registry first.")
            return record, None
        sources = claim_sources(graph, node.id)
        if not sources:
            raise SourceError(
                f"Claim {ref_id!r} has no source paper; unsourced claims cannot be cited."
            )
        record = self.get(project_id, sources[0])
        if record is None:
            raise SourceError(f"Source {sources[0]!r} for claim {ref_id!r} is not registered.")
        return record, claim_info(node, record)


def claim_info(node: Any, source: Dict[str, Any]) -> Dict[str, Any]:
    metadata = node.metadata or {}
    return {
        "claim_id": node.id,
        "text": node.content,
        "node_type": node.node_type,
        "source_id": source["source_id"],
        "page": metadata.get("page"),
        "section": metadata.get("section"),
        "origin": str(metadata.get("origin") or CLAIM_ORIGIN_DEFAULT),
        "review_status": str(metadata.get("review_status") or "unreviewed"),
        "status": node_status(node),
        "content_hash": stable_hash({"content": node.content, "status": node_status(node)}, length=64),
    }


_BIB_SPECIAL = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "^": r"\textasciicircum{}",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "%": r"\%",
}


def bib_escape(text: str) -> str:
    cleaned = "".join(ch for ch in str(text or "") if ch == "\n" or ord(ch) >= 32)
    return "".join(_BIB_SPECIAL.get(ch, ch) for ch in cleaned).replace("\n", " ")


def bibtex_entry(record: Dict[str, Any]) -> str:
    fields: List[Tuple[str, str]] = [("title", "{" + bib_escape(record.get("title") or record["source_id"]) + "}")]
    authors = record.get("authors") or []
    if authors:
        fields.append(("author", " and ".join(bib_escape(a) for a in authors)))
    if record.get("year"):
        fields.append(("year", str(record["year"])))
    if record.get("venue"):
        fields.append(("journal", bib_escape(record["venue"])))
    if record.get("doi"):
        fields.append(("doi", bib_escape(record["doi"])))
    if record.get("arxiv_id"):
        fields.extend([("eprint", bib_escape(record["arxiv_id"])), ("archiveprefix", "arXiv")])
    if record.get("url"):
        fields.append(("howpublished", "\\url{" + bib_escape(record["url"]) + "}"))
    fields.append(("note", bib_escape(f"ResearchOS source {record['source_id']} v{record.get('version', 1)}")))
    entry_type = "article" if record.get("venue") else "misc"
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry_type}{{{record['citation_key']},\n{body}\n}}\n"


def bibtex(records: Iterable[Dict[str, Any]]) -> str:
    return "\n".join(bibtex_entry(r) for r in sorted(records, key=lambda r: r["citation_key"]))
