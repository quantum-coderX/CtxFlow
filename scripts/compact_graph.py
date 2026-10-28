"""Run offline graph compaction.

Usage:
    python scripts/compact_graph.py input.json output.json
    python scripts/compact_graph.py input.json output.json --strategy drop
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ctxflow.maintenance import compact_graph
from ctxflow.serialization import export_json, import_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument(
        "--strategy",
        choices=("drop", "summarize"),
        default="summarize",
    )
    args = parser.parse_args()
    if not 0 <= args.threshold <= 1:
        raise SystemExit("--threshold must be between 0 and 1")

    graph = import_json(str(args.input))
    result = compact_graph(
        graph,
        threshold=args.threshold,
        strategy=args.strategy,
    )
    export_json(graph, str(args.output))
    print(
        json.dumps(
            {
                "nodes_before": result.nodes_before,
                "nodes_after": result.nodes_after,
                "removed": len(result.removed_node_ids),
                "strategy": args.strategy,
                "threshold": args.threshold,
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
