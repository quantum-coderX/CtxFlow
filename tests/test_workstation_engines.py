"""Chart, artifact, proof and OCR engines plus their harness integration."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from ctxflow import CtxFlow
from researchos import proof as proof_module
from researchos.artifacts import (
    ArtifactError,
    CompileResult,
    compose_report_spec,
    latex_escape,
    validate_equation,
)
from researchos.charts import ChartError
from researchos.harness import ToolHarness, ToolTask
from researchos.skills.registry import SkillRegistry
from researchos.workstation import Workstation

HOSTILE = r"\input{/etc/passwd} \write18{rm -rf /} $x$ & # % _ ^ ~ {} \\"


def _unavailable_compiler(build_dir, *, has_bibliography):
    return CompileResult(status="unavailable", engine=None, engine_version=None,
                         log_excerpt="", duration_ms=0.0, error="no TeX")


@pytest.fixture
def station(tmp_path):
    return Workstation(tmp_path, compiler=_unavailable_compiler)


@pytest.fixture
def graph():
    cf = CtxFlow()
    paper = cf.ingest(
        content="Paper: Attention is all you need", node_type="paper",
        metadata={"project_id": "p1", "title": "Attention " + HOSTILE, "authors": ["Ashish Vaswani"],
                  "year": 2017, "arxiv_id": "1706.03762"},
    )
    claim = cf.ingest(
        content="Transformers beat recurrent models on WMT14 " + HOSTILE, node_type="claim",
        source_id=paper, relation_type="extracted_from", metadata={"project_id": "p1", "page": 8},
    )
    cf.paper_id, cf.claim_id = paper, claim
    return cf


def _scores(station, values=("24.1", "24.9", "28.4", "27.9")):
    rows = zip(["rnn", "rnn", "transformer", "transformer"], values)
    text = "model,bleu\n" + "".join(f"{m},{v}\n" for m, v in rows)
    return station.datasets.ingest("p1", name="scores", filename="scores.csv", payload=text.encode())


# -- Charts -------------------------------------------------------------------


def test_bar_chart_plots_engine_computed_means_and_is_reproducible(station):
    _scores(station)
    spec = {"chart_type": "bar", "dataset_id": "scores", "x": "model", "y": "bleu", "aggregate": "mean"}
    chart = station.charts.create("p1", spec)

    assert chart["data_used"]["series"][0]["values"] == pytest.approx([24.5, 28.15])
    assert chart["dataset"]["version"] == 1
    svg = station.charts.file_path("p1", chart["chart_id"], "svg").read_bytes()
    again = station.charts.create("p1", spec)
    assert again["chart_id"] == chart["chart_id"]
    assert station.charts.file_path("p1", chart["chart_id"], "svg").read_bytes() == svg


@pytest.mark.parametrize(
    "spec, message",
    [
        ({"chart_type": "bar", "dataset_id": "scores", "x": "model", "y": "bleu"}, "aggregate"),
        ({"chart_type": "line", "dataset_id": "scores", "x": "model", "y": "bleu"}, "continuous axis"),
        ({"chart_type": "heatmap", "dataset_id": "scores"}, "analysis_id"),
        ({"chart_type": "pie", "dataset_id": "scores"}, "chart_type"),
    ],
)
def test_invalid_chart_specs_are_rejected(station, spec, message):
    _scores(station)
    with pytest.raises(ChartError, match=message):
        station.charts.create("p1", spec)


# -- Artifacts ------------------------------------------------------------------


def test_untrusted_text_is_escaped_in_latex_and_bibtex(station, graph):
    spec, _ = compose_report_spec("p1", graph, topic="transformers")
    record = station.artifacts.create("p1", graph, spec)
    tex = station.artifacts.file_path("p1", record["artifact_id"], "latex").read_text()
    bib = station.artifacts.file_path("p1", record["artifact_id"], "bibtex").read_text()

    for raw in (r"\input{", r"\write18{", "$x$"):
        assert raw not in tex and raw not in bib
    assert latex_escape(HOSTILE) in tex
    assert r"\cite{vaswani2017attention}" in tex


def test_build_without_tex_is_partial_with_explicit_pdf_status(station, graph):
    spec, _ = compose_report_spec("p1", graph)
    record = station.artifacts.create("p1", graph, spec)
    build = record["builds"][-1]

    assert record["status"] == "partial"
    assert build["outputs"]["pdf"]["status"] == "unavailable"
    assert {build["outputs"][f]["status"] for f in ("markdown", "html", "latex")} == {"ok"}
    assert build["dependencies"]["claims"].keys() == {graph.claim_id}


def test_unresolved_citation_fails_the_build(station, graph):
    spec = {"title": "T", "sections": [{"heading": "S", "blocks": [
        {"type": "paragraph", "text": "Unsupported statement [@missing-id]."}]}]}
    record = station.artifacts.create("p1", graph, spec)

    assert record["status"] == "error"
    assert "missing-id" in record["builds"][-1]["errors"][0]
    assert all(o["status"] == "skipped" for o in record["builds"][-1]["outputs"].values())


def test_unsourced_claim_cannot_be_cited(station, graph):
    orphan = graph.ingest(content="An orphan claim with no paper.", node_type="claim", metadata={"project_id": "p1"})
    spec = {"title": "T", "sections": [{"heading": "S", "blocks": [{"type": "claim", "claim_id": orphan}]}]}
    assert station.artifacts.create("p1", graph, spec)["status"] == "error"


@pytest.mark.parametrize("latex", [r"\input{x}", r"\def\a{b}", "x # y", r"\frac{1}{2", r"\immediate\write18{ls}"])
def test_equation_policy_rejects_tex_side_effects(latex):
    with pytest.raises(ArtifactError):
        validate_equation(latex)


def test_rebuild_is_reproducible_and_dataset_change_marks_report_stale(station, graph):
    _scores(station)
    analysis = station.analyses.run("p1", "scores", "group_aggregate",
                                    {"group_by": ["model"], "aggregations": [{"column": "bleu", "func": "mean"}]})
    chart = station.charts.create("p1", {"chart_type": "bar", "dataset_id": "scores", "x": "model",
                                         "y": "bleu", "aggregate": "mean"})
    spec, _ = compose_report_spec("p1", graph, analysis_ids=[analysis["analysis_id"]], chart_ids=[chart["chart_id"]])
    record = station.artifacts.create("p1", graph, spec)
    artifact_id = record["artifact_id"]
    station.artifacts.build("p1", artifact_id, graph)

    digest = lambda v: hashlib.sha256(station.artifacts.file_path("p1", artifact_id, "latex", v).read_bytes()).hexdigest()  # noqa: E731
    assert digest(1) == digest(2)
    assert station.artifacts.staleness("p1", artifact_id, graph)["stale"] is False

    _scores(station, values=("24.1", "24.9", "28.4", "30.0"))
    stale = station.artifacts.staleness("p1", artifact_id, graph)
    assert stale["stale"] and "newer version" in stale["reasons"][0]

    rebuilt = station.artifacts.build("p1", artifact_id, graph, refresh_inputs=True)
    assert set(rebuilt["builds"][-1]["refreshed"]) == {analysis["analysis_id"], chart["chart_id"]}
    assert station.artifacts.staleness("p1", artifact_id, graph)["stale"] is False
    markdown = station.artifacts.file_path("p1", artifact_id, "markdown").read_text()
    assert "scores v2" in markdown and "29.2" in markdown


def test_source_change_marks_report_stale(station, graph):
    spec, _ = compose_report_spec("p1", graph)
    artifact_id = station.artifacts.create("p1", graph, spec)["artifact_id"]
    graph.graph.get_node(graph.claim_id).content = "Revised claim text."

    reasons = station.artifacts.staleness("p1", artifact_id, graph)["reasons"]
    assert any(graph.claim_id in reason for reason in reasons)


def test_successful_compile_records_pdf_and_compiler(tmp_path, graph):
    def fake_compiler(build_dir, *, has_bibliography):
        assert (build_dir / "main.tex").exists() and (build_dir / "refs.bib").exists()
        return CompileResult(status="ok", engine="tectonic", engine_version="tectonic 0.15",
                             log_excerpt="ok", duration_ms=5.0, pdf=b"%PDF-1.7 fake")

    station = Workstation(tmp_path, compiler=fake_compiler)
    spec, _ = compose_report_spec("p1", graph)
    record = station.artifacts.create("p1", graph, spec)

    assert record["status"] == "ok"
    assert record["builds"][-1]["outputs"]["pdf"]["compiler"]["shell_escape"] is False
    assert station.artifacts.file_path("p1", record["artifact_id"], "pdf").read_bytes() == b"%PDF-1.7 fake"


def test_model_synthesis_citing_unknown_ids_is_discarded(graph):
    spec, warnings = compose_report_spec("p1", graph, llm=lambda prompt, system: "Made up [@not-real].")
    assert all(s["heading"] != "Synthesis" for s in spec["sections"])
    assert any("unknown ids" in w for w in warnings)

    ok_spec, _ = compose_report_spec("p1", graph, llm=lambda prompt, system: f"Supported [@{graph.claim_id}].")
    assert any(s["heading"] == "Synthesis" for s in ok_spec["sections"])


# -- Proof checking -------------------------------------------------------------


@pytest.mark.parametrize("source", ["#eval IO.println 1", "run_cmd pure ()", "unsafe def x : Nat := 1",
                                    "@[implemented_by foo] def bar : Nat := 1", "import Lean"])
def test_proof_checker_rejects_code_execution(station, source):
    result = station.proofs.check({"source": source})
    assert result["status"] == "rejected" and result["verified"] is False


def test_proof_checker_reports_unavailable_without_lean(station, monkeypatch):
    monkeypatch.setattr(proof_module, "detect_lean", lambda: {"lean": None, "lake": None})
    result = station.proofs.check({"source": "theorem t : 1 + 1 = 2 := rfl", "declaration": "t"})
    assert result["status"] == "unavailable" and result["verified"] is False


def _fake_lean(monkeypatch, messages):
    monkeypatch.setattr(proof_module, "detect_lean", lambda: {"lean": "lean", "lake": None})
    monkeypatch.setattr(proof_module, "_version", lambda lean, env: "Lean (version 4.9.0)")
    stdout = "\n".join(json.dumps(m) for m in messages).encode()
    monkeypatch.setattr(proof_module.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=b""))


def test_proof_verified_only_with_standard_axioms(station, monkeypatch):
    _fake_lean(monkeypatch, [{"severity": "information", "pos": {"line": 3, "column": 0},
                              "data": "'t' depends on axioms: [propext]"}])
    result = station.proofs.check({"source": "theorem t : True := trivial", "declaration": "t"}, project_id="p1")
    assert result["status"] == "verified" and result["axioms"] == ["propext"]
    assert result["toolchain"] == "Lean (version 4.9.0)"
    assert station.proofs.list("p1")[0]["check_id"] == result["check_id"]


def test_proof_with_sorry_fails(station, monkeypatch):
    _fake_lean(monkeypatch, [
        {"severity": "warning", "pos": {"line": 1, "column": 8}, "data": "declaration uses 'sorry'"},
        {"severity": "information", "pos": {"line": 3, "column": 0}, "data": "'t' depends on axioms: [sorryAx]"},
    ])
    result = station.proofs.check({"source": "theorem t : False := sorry", "declaration": "t"})
    assert result["status"] == "failed" and "sorry" in result["reason"]


# -- OCR / documents -----------------------------------------------------------


def _pdf_bytes():
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Results: the transformer reached 28.4 BLEU on WMT14 English-German.")
    document.new_page()
    payload = document.tobytes()
    document.close()
    return payload


def test_text_layer_extraction_keeps_pages_and_flags_blank_pages(station, monkeypatch):
    from researchos import ocr

    monkeypatch.setattr(ocr, "engine_availability", lambda: {"text_layer": True, "tesseract": False, "nougat": False})
    document = station.documents.add("p1", filename="paper.pdf", payload=_pdf_bytes())
    result = station.documents.extract("p1", document["document_id"])

    assert result["status"] == "partial"
    assert [(p["page"], p["method"]) for p in result["pages"]] == [(1, "text_layer"), (2, "no_text")]
    assert "28.4 BLEU" in result["pages"][0]["text"]

    unavailable = station.documents.extract("p1", document["document_id"], {"engine": "nougat"})
    assert unavailable["status"] == "unavailable"


def test_non_pdf_documents_are_rejected(station):
    with pytest.raises(ValueError, match="Only PDF"):
        station.documents.add("p1", filename="x.pdf", payload=b"<html>not a pdf</html>")


# -- Harness integration ---------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_runs_dataset_analysis_chart_report_plan(station, graph):
    registry = SkillRegistry()
    station.register_skills(registry, graph_for=lambda project_id: graph)
    harness = ToolHarness(registry)

    result = await harness.run([
        ToolTask("ingest", "dataset_ingest", {"project_id": "p1", "name": "scores",
                                              "content": "model,bleu\nrnn,24\nrnn,25\nt,28\nt,29\n"}),
        ToolTask("stats", "dataset_analyze", {
            "project_id": "p1", "dataset_id": {"$ref": "ingest.dataset_id"}, "operation": "group_aggregate",
            "params": {"group_by": ["model"], "aggregations": [{"column": "bleu", "func": "mean"}]},
        }, depends_on=("ingest",)),
        ToolTask("chart", "create_chart", {"project_id": "p1", "spec": {
            "chart_type": "box", "dataset_id": "scores", "x": "model", "y": "bleu"}}, depends_on=("ingest",)),
        ToolTask("report", "document_compose", {
            "project_id": "p1", "analysis_ids": [{"$ref": "stats.analysis_id"}],
            "chart_ids": [{"$ref": "chart.chart_id"}], "formats": ["markdown", "latex"],
        }, depends_on=("stats", "chart")),
        ToolTask("bad", "dataset_analyze", {"project_id": "p1", "dataset_id": "scores", "operation": "eval"}),
    ])

    statuses = {task.task_id: task.status for task in result.tasks}
    assert statuses == {"ingest": "completed", "stats": "completed", "chart": "completed",
                        "report": "completed", "bad": "failed"}
    assert result.status == "partial"
    report = result.tasks[3].data
    assert report["status"] == "ok"
    deps = report["builds"][-1]["dependencies"]
    assert result.tasks[1].data["analysis_id"] in deps["analyses"]
    assert result.tasks[2].data["chart_id"] in deps["charts"]
