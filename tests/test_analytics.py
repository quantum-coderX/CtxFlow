"""Dataset registry and analytics catalog: correctness, versioning, validation."""

from __future__ import annotations

import json
import math
import statistics

import pytest

from researchos.analytics import AnalysisEngine, AnalyticsError, DatasetRegistry
from researchos.project_store import ProjectStore


@pytest.fixture
def engines(tmp_path):
    registry = DatasetRegistry(ProjectStore(tmp_path))
    return registry, AnalysisEngine(registry)


def ingest(registry, text, name="data", project="p1", **kwargs):
    return registry.ingest(project, name=name, filename=f"{name}.csv", payload=text.encode(), **kwargs)


REGRESSION_CSV = "x,y\n1,2\n2,4\n3,5\n4,4\n5,5\n"


def test_ingest_infers_types_records_provenance_and_reuses_identical_upload(engines):
    registry, _ = engines
    record = ingest(registry, "id,score,ok,when,label\n1,2.5,true,2024-01-02,a\n2,,false,2024-01-03,b\n")

    assert record["version"] == 1
    assert {c["name"]: c["dtype"] for c in record["schema"]} == {
        "id": "integer", "score": "number", "ok": "boolean", "when": "datetime", "label": "string",
    }
    assert len(record["content_hash"]) == 64
    assert record["provenance"]["filename"] == "data.csv"
    score = next(c for c in record["profile"]["columns"] if c["name"] == "score")
    assert score["missing"] == 1
    assert any("50% missing" in w for w in record["warnings"])

    again = ingest(registry, "id,score,ok,when,label\n1,2.5,true,2024-01-02,a\n2,,false,2024-01-03,b\n")
    assert again["reused"] is True and again["version"] == 1

    changed = ingest(registry, "id,score,ok,when,label\n1,3.5,true,2024-01-02,a\n")
    assert changed["version"] == 2
    assert registry.version("p1", "data", 1)["row_count"] == 2


def test_parse_errors_are_explicit_not_silent(engines):
    registry, _ = engines
    with pytest.raises(AnalyticsError, match="Row 3 has 1 fields"):
        ingest(registry, "a,b\n1,2\n3\n")
    with pytest.raises(AnalyticsError, match="not a valid integer"):
        ingest(registry, "a\n1\nx\n", column_types={"a": "integer"})
    mostly = ingest(registry, "a\n" + "\n".join(["1"] * 12 + ["oops"]) + "\n", name="mostly")
    assert mostly["schema"][0]["dtype"] == "string"
    assert any("non-numeric" in w for w in mostly["warnings"])


def test_describe_matches_reference_values(engines):
    registry, analyses = engines
    ingest(registry, "v\n2\n4\n4\n4\n5\n5\n7\n9\n")
    result = analyses.run("p1", "data", "describe", {})
    row = dict(zip(result["tables"][0]["columns"], result["tables"][0]["rows"][0]))

    assert row["n"] == 8
    assert row["mean"] == pytest.approx(5.0)
    assert row["std"] == pytest.approx(math.sqrt(32 / 7))
    assert row["median"] == pytest.approx(4.5)
    assert row["sem"] == pytest.approx(math.sqrt(32 / 7) / math.sqrt(8))


def test_linear_regression_and_correlation_match_textbook_example(engines):
    registry, analyses = engines
    ingest(registry, REGRESSION_CSV)

    ols = analyses.run("p1", "data", "linear_regression", {"y": "y", "x": ["x"]})
    coef = {row[0]: dict(zip(ols["tables"][0]["columns"], row)) for row in ols["tables"][0]["rows"]}
    assert coef["intercept"]["estimate"] == pytest.approx(2.2)
    assert coef["x"]["estimate"] == pytest.approx(0.6)
    assert coef["x"]["std_error"] == pytest.approx(math.sqrt(0.8 / 10))
    assert ols["values"]["r_squared"] == pytest.approx(0.6)
    assert ols["values"]["df_resid"] == 3

    corr = analyses.run("p1", "data", "correlation", {"columns": ["x", "y"]})
    assert corr["tables"][0]["rows"][0][2] == pytest.approx(math.sqrt(0.6))


def test_one_sample_t_test_matches_manual_formula(engines):
    registry, analyses = engines
    values = [5.1, 4.9, 5.6, 5.8, 6.0, 5.4]
    ingest(registry, "v\n" + "\n".join(map(str, values)) + "\n")
    result = analyses.run("p1", "data", "t_test", {"kind": "one_sample", "column": "v", "mu": 5.0})

    expected_t = (statistics.mean(values) - 5.0) / (statistics.stdev(values) / math.sqrt(len(values)))
    assert result["values"]["statistic"] == pytest.approx(expected_t)
    assert result["values"]["df"] == pytest.approx(5)
    assert result["values"]["ci_lower"] < statistics.mean(values) < result["values"]["ci_upper"]


def test_same_version_and_params_reproduce_identical_numbers(engines):
    registry, analyses = engines
    ingest(registry, REGRESSION_CSV)
    first = analyses.run("p1", "data", "linear_regression", {"x": ["x"], "y": "y"})
    second = analyses.run("p1", "data", "linear_regression", {"y": "y", "x": ["x"]})

    assert first["analysis_id"] == second["analysis_id"]
    assert json.dumps(first["values"], sort_keys=True) == json.dumps(second["values"], sort_keys=True)
    assert first["tables"] == second["tables"]
    assert first["dataset"] == {"dataset_id": "data", "version": 1, "content_hash": first["dataset"]["content_hash"]}
    assert set(first["environment"]) >= {"engine", "numpy", "scipy"}


@pytest.mark.parametrize(
    "operation, params, message",
    [
        ("exec_python", {}, "Unknown operation"),
        ("describe", {"columns": ["x"], "code": "import os"}, "Extra inputs"),
        ("correlation", {"columns": ["x"]}, "at least 2"),
        ("t_test", {"kind": "independent", "column": "y"}, "group_column"),
    ],
)
def test_invalid_requests_are_rejected_before_execution(engines, operation, params, message):
    registry, analyses = engines
    ingest(registry, REGRESSION_CSV)
    with pytest.raises(AnalyticsError, match=message):
        analyses.run("p1", "data", operation, params)


def test_non_numeric_column_is_rejected(engines):
    registry, analyses = engines
    ingest(registry, "g,v\na,1\nb,2\n")
    with pytest.raises(AnalyticsError, match="needs a numeric column"):
        analyses.run("p1", "data", "describe", {"columns": ["g"]})


def test_transform_creates_derived_version_and_keeps_parent_intact(engines):
    registry, analyses = engines
    ingest(registry, "g,v\na,1\na,\nb,3\nb,5\n")
    derived = registry.transform("p1", "data", [
        {"op": "fill_missing", "column": "v", "strategy": "mean"},
        {"op": "filter_rows", "column": "g", "operator": "==", "value": "b"},
    ])

    assert derived["version"] == 2
    assert derived["provenance"]["derived_from"]["version"] == 1
    assert [s["op"] for s in derived["provenance"]["derived_from"]["steps"]] == ["fill_missing", "filter_rows"]
    assert any("fill_missing filled 1" in w for w in derived["warnings"])
    assert registry.preview("p1", "data", 1)["total_rows"] == 4
    assert [r["v"] for r in registry.preview("p1", "data", 2)["rows"]] == [3.0, 5.0]

    with pytest.raises(AnalyticsError, match="op must be one of"):
        registry.transform("p1", "data", [{"op": "python", "code": "1"}])


def test_join_records_joined_dataset_version(engines):
    registry, _ = engines
    ingest(registry, "id,v\n1,10\n2,20\n3,30\n", name="left")
    ingest(registry, "id,label\n1,a\n2,b\n", name="right")
    joined = registry.transform("p1", "left", [{"op": "join", "dataset_id": "right", "on": ["id"], "how": "left"}])

    assert joined["provenance"]["derived_from"]["joined"][0]["dataset_id"] == "right"
    assert [r["label"] for r in registry.preview("p1", "left")["rows"]] == ["a", "b", None]


def test_group_aggregate_anova_and_chi_square(engines):
    registry, analyses = engines
    ingest(registry, "g,v,c\na,1,x\na,2,y\na,3,x\nb,4,y\nb,5,x\nb,6,y\n")
    groups = analyses.run("p1", "data", "group_aggregate",
                          {"group_by": ["g"], "aggregations": [{"column": "v", "func": "mean"}]})
    assert groups["tables"][0]["rows"] == [["a", 3, 2.0], ["b", 3, 5.0]]

    anova = analyses.run("p1", "data", "anova_oneway", {"column": "v", "group_column": "g"})
    # SS_between = 13.5, SS_within = 4, F = 13.5 / (4 / 4)
    assert anova["values"]["statistic"] == pytest.approx(13.5)
    assert anova["values"]["eta_squared"] == pytest.approx(13.5 / 17.5)

    chi = analyses.run("p1", "data", "chi_square", {"row_column": "g", "col_column": "c"})
    assert chi["values"]["df"] == 1
    assert any("expected counts" in w for w in chi["warnings"])


def test_tampered_dataset_fails_integrity_check(engines, tmp_path):
    registry, analyses = engines
    ingest(registry, "v\n1\n2\n3\n")
    table_file = tmp_path / "projects" / "p1" / "datasets" / "data" / "v1" / "table.json"
    payload = json.loads(table_file.read_text())
    payload["data"]["v"][0] = 100
    table_file.write_text(json.dumps(payload))
    registry._cache.clear()

    with pytest.raises(AnalyticsError, match="integrity"):
        analyses.run("p1", "data", "describe", {})


def test_projects_are_isolated(engines):
    registry, analyses = engines
    ingest(registry, "v\n1\n2\n", project="alpha")
    assert registry.list("beta") == []
    with pytest.raises(AnalyticsError, match="not found"):
        analyses.run("beta", "data", "describe", {})
