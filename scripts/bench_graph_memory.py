"""Measure the heap footprint of the current networkx-backed ContextGraph.

Builds the same graph shape as ``rust/ctxflow-core/examples/mem_bench.rs``
(same node keys, relations and deterministic edge generator) so the two
numbers are directly comparable:

    python scripts/bench_graph_memory.py --nodes 100000 --degree 8
    cargo run --release --example mem_bench -- 100000 8   # in rust/

Python node objects carry content/tags/metadata that the Rust core keeps
outside the hot graph; content is left empty here to keep the comparison
about graph structure.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
import tracemalloc

from ctxflow.graph import ContextGraph
from ctxflow.models import Edge, Node

_RELATIONS = ("follows", "derived_from", "causes", "mentions")
_MASK64 = (1 << 64) - 1


class _Lcg:
    def __init__(self, seed: int) -> None:
        self.state = seed

    def next(self) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) & _MASK64
        return self.state >> 33


def run(nodes: int, degree: int) -> dict[str, float | int | str]:
    key = "node-{:08d}".format
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]

    started = time.perf_counter()
    graph = ContextGraph()
    for i in range(nodes):
        graph.add_node(Node(content="", id=key(i), timestamp=float(i)))
    rng = _Lcg(42)
    for i in range(nodes):
        for j in range(degree):
            dst = rng.next() % nodes
            graph.add_edge(
                Edge(key(i), key(dst), relation_type=_RELATIONS[j % len(_RELATIONS)], timestamp=float(i))
            )
    ingest_s = time.perf_counter() - started

    gc.collect()
    heap = tracemalloc.get_traced_memory()[0] - baseline
    tracemalloc.stop()
    edges = graph.edge_count
    return {
        "engine": "networkx ContextGraph",
        "nodes": nodes,
        "edges": edges,
        "heap_bytes": heap,
        "bytes_per_edge": round(heap / max(edges, 1), 1),
        "ingest_s": round(ingest_s, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--degree", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(run(args.nodes, args.degree), indent=2))


if __name__ == "__main__":
    main()
