"""Ingest the checked-in demo corpus and report reproducible graph statistics.

Usage:
    python scripts/demo_corpus.py
    python scripts/demo_corpus.py --corpus data/ctxflow_demo_corpus.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ctxflow import CtxFlow, CtxFlowConfig
from ctxflow.serialization import export_json


def load_corpus(path: Path) -> list[dict[str, Any]]:
    """Load and validate JSONL corpus records."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"corpus file does not exist: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on corpus line {line_number}") from error
        if not isinstance(record, dict):
            raise ValueError(f"corpus line {line_number} must be an object")
        if not isinstance(record.get("id"), str) or not record["id"].strip():
            raise ValueError(f"corpus line {line_number} requires a non-empty id")
        if not isinstance(record.get("content"), str) or not record["content"].strip():
            raise ValueError(f"corpus line {line_number} requires content")
        records.append(record)
    if not records:
        raise ValueError("corpus must contain at least one record")
    return records


def ingest_corpus(records: list[dict[str, Any]]) -> CtxFlow:
    """Ingest records with deterministic graph configuration."""

    ctx = CtxFlow(
        config=CtxFlowConfig(
            max_link_k=3,
            min_link_similarity=0.1,
            degree_cap=10,
        )
    )
    previous_id: str | None = None
    for record in records:
        previous_id = ctx.ingest(
            record["content"],
            node_type=record.get("node_type"),
            source_id=previous_id,
            relation_type="follows",
            tags=set(record.get("tags", [])),
            metadata={"demo_id": record["id"]},
        )
    return ctx


def graph_stats(ctx: CtxFlow, records: list[dict[str, Any]]) -> dict[str, int | float]:
    """Return deterministic logical and serialized footprint statistics."""

    content_bytes = sum(len(record["content"].encode("utf-8")) for record in records)
    tag_count = sum(len(record.get("tags", [])) for record in records)
    with TemporaryDirectory() as directory:
        output = Path(directory) / "graph.json"
        export_json(ctx.graph, str(output))
        serialized_bytes = output.stat().st_size
    return {
        "corpus_records": len(records),
        "nodes": ctx.graph.node_count,
        "edges": ctx.graph.edge_count,
        "tag_assignments": tag_count,
        "content_bytes": content_bytes,
        "serialized_bytes": serialized_bytes,
        "serialized_bytes_per_node": round(
            serialized_bytes / ctx.graph.node_count, 2
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("data/ctxflow_demo_corpus.jsonl"),
    )
    args = parser.parse_args()
    try:
        records = load_corpus(args.corpus)
        ctx = ingest_corpus(records)
        print(json.dumps(graph_stats(ctx, records), sort_keys=True))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
