"""Benchmark graph ingestion and query latency.

The default run measures the S4 target workload:

    python scripts/bench_graph.py

Use ``--enforce-budget`` in CI or a release check to fail when either P95
target is exceeded.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time

from ctxflow import CtxFlow, CtxFlowConfig

_QUERY_P95_BUDGET_MS = 150.0
_INGEST_P95_BUDGET_MS = 50.0


def _p95(samples: list[float]) -> float:
    if not samples:
        raise ValueError("benchmark requires at least one sample")
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def run_benchmark(
    *,
    nodes: int = 10_000,
    iterations: int = 50,
) -> dict[str, float | int | bool]:
    """Run the benchmark and return machine-readable latency statistics."""

    if nodes < 1 or iterations < 1:
        raise ValueError("nodes and iterations must be positive")

    ctx = CtxFlow(
        config=CtxFlowConfig(
            max_link_k=0,
            degree_cap_enabled=False,
        )
    )
    ingest_samples: list[float] = []
    for index in range(nodes):
        started = time.perf_counter()
        ctx.ingest(
            f"benchmark memory {index} about graph retrieval",
            tags={"benchmark", "retrieval"},
        )
        ingest_samples.append((time.perf_counter() - started) * 1000)

    query_samples: list[float] = []
    for index in range(iterations):
        started = time.perf_counter()
        results = ctx.query("graph retrieval", k=5)
        query_samples.append((time.perf_counter() - started) * 1000)
        if len(results) != 5:
            raise RuntimeError("benchmark query returned an unexpected result count")

    ingest_p95 = _p95(ingest_samples)
    query_p95 = _p95(query_samples)
    return {
        "nodes": nodes,
        "query_iterations": iterations,
        "ingest_p95_ms": round(ingest_p95, 3),
        "query_p95_ms": round(query_p95, 3),
        "ingest_mean_ms": round(statistics.fmean(ingest_samples), 3),
        "query_mean_ms": round(statistics.fmean(query_samples), 3),
        "ingest_budget_ms": _INGEST_P95_BUDGET_MS,
        "query_budget_ms": _QUERY_P95_BUDGET_MS,
        "within_budget": (
            ingest_p95 < _INGEST_P95_BUDGET_MS
            and query_p95 < _QUERY_P95_BUDGET_MS
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=10_000)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--enforce-budget", action="store_true")
    args = parser.parse_args()
    try:
        result = run_benchmark(nodes=args.nodes, iterations=args.iterations)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, sort_keys=True))
    if args.enforce_budget and not result["within_budget"]:
        raise SystemExit("performance budget exceeded")


if __name__ == "__main__":
    main()
