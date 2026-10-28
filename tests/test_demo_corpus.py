import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.demo_corpus import graph_stats, ingest_corpus, load_corpus


def test_demo_corpus_ingests_and_reports_graph_stats():
    records = load_corpus(Path("data/ctxflow_demo_corpus.jsonl"))
    stats = graph_stats(ingest_corpus(records), records)

    assert stats["corpus_records"] == 12
    assert stats["nodes"] == 12
    assert stats["edges"] > 0
    assert stats["content_bytes"] > 0
    assert stats["serialized_bytes"] > stats["content_bytes"]


def test_demo_corpus_cli_reports_json():
    completed = subprocess.run(
        [sys.executable, "scripts/demo_corpus.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    stats = json.loads(completed.stdout)
    assert stats["corpus_records"] == 12
    assert stats["nodes"] == 12


def test_demo_corpus_missing_file_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="corpus file does not exist"):
        load_corpus(tmp_path / "missing.jsonl")
