"""Append real arXiv papers to the checked-in demo corpus.

The demo corpus is committed JSON so the demo never depends on the network.
This script is only for regenerating it:

    python scripts/expand_demo_corpus.py

It fetches metadata (title, authors, year, abstract) for the arXiv IDs below
via the same public API the ``literature_search`` skill uses, and appends any
paper that is not already present (deduplicated by arXiv ID).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "src" / "researchos" / "demo" / "corpus.json"

# Extra papers chosen for one coherent sub-literature: retrieval-augmented
# generation, graph memory, and long-context agents. All IDs were verified
# against the arXiv API when this list was written.
EXTRA_ARXIV_IDS = [
    "2310.08560",  # MemGPT
    "2410.05779",  # LightRAG
    "2408.08921",  # Graph RAG survey
    "2501.09136",  # Agentic RAG survey
    "2403.14403",  # Adaptive-RAG
    "2310.01061",  # Reasoning on Graphs (RoG)
    "2508.05498",  # GRAIL
    "2502.17129",  # Thus Spake Long-Context LLM
    "2510.22344",  # FAIR-RAG
    "2602.06052",  # Survey of Agent Memory
]

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_NS = {"arxiv": "http://arxiv.org/schemas/atom"}


def fetch(arxiv_ids: list[str]) -> list[dict]:
    from xml.etree import ElementTree

    query = urllib.parse.urlencode(
        {"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)}
    )
    url = f"http://export.arxiv.org/api/query?{query}"
    with urllib.request.urlopen(url, timeout=60) as response:
        xml = response.read().decode("utf-8")
    root = ElementTree.fromstring(xml)

    papers = []
    for entry in root.findall("atom:entry", ATOM_NS):
        arxiv_url = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        arxiv_id = arxiv_url.rsplit("/", 1)[-1].split("v")[0]
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": " ".join(
                    (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").split()
                ),
                "authors": [
                    (author.findtext("atom:name", default="", namespaces=ATOM_NS) or "").strip()
                    for author in entry.findall("atom:author", ATOM_NS)
                ],
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "abstract": " ".join(
                    (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").split()
                ),
            }
        )
    return papers


def main() -> int:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    existing_ids = {paper.get("arxiv_id") for paper in corpus}
    existing_titles = {
        (paper.get("title") or "").strip().lower() for paper in corpus
    }

    missing = [i for i in EXTRA_ARXIV_IDS if i not in existing_ids]
    if not missing:
        print("Corpus already contains every requested paper.")
        return 0

    fetched = fetch(missing)
    added = 0
    for paper in fetched:
        if paper["title"].lower() in existing_titles:
            continue
        if not paper["abstract"]:
            print(f"Skipping {paper['arxiv_id']}: empty abstract")
            continue
        corpus.append(paper)
        existing_titles.add(paper["title"].lower())
        added += 1
        print(f"+ {paper['arxiv_id']} {paper['year']} {paper['title'][:70]}")

    CORPUS.write_text(
        json.dumps(corpus, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Added {added} paper(s); corpus now has {len(corpus)} entries.")
    return 0 if added else 1


if __name__ == "__main__":
    sys.exit(main())
