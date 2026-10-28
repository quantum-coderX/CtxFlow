import json
import subprocess
import sys

from ctxflow.extractors import DefaultExtractor
from ctxflow.graph import ContextGraph
from ctxflow.maintenance import compact_graph
from ctxflow.models import Node
from ctxflow.serialization import export_json


def test_compact_graph_is_explicit_and_reports_node_counts():
    graph = ContextGraph()
    graph.add_node(Node(id="old", content="old", timestamp=1.0))
    result = compact_graph(
        graph,
        threshold=0.0,
        strategy="drop",
        extractor=DefaultExtractor(),
    )

    assert result.nodes_before == 1
    assert result.nodes_after == 0
    assert result.removed_node_ids == ("old",)


def test_compaction_cli_writes_compacted_graph(tmp_path):
    source = tmp_path / "source.json"
    target = tmp_path / "target.json"
    graph = ContextGraph()
    graph.add_node(Node(id="old", content="old", timestamp=1.0))
    export_json(graph, str(source))

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/compact_graph.py",
            str(source),
            str(target),
            "--strategy",
            "drop",
            "--threshold",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert report["nodes_before"] == 1
    assert report["nodes_after"] == 0
    assert target.exists()


def test_benchmark_reports_budget_status_for_small_workload():
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/bench_graph.py",
            "--nodes",
            "20",
            "--iterations",
            "3",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert report["nodes"] == 20
    assert report["query_iterations"] == 3
    assert report["within_budget"] is True
