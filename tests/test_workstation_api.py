"""HTTP API for datasets, analyses, charts, artifacts, documents and proofs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from researchos.api.server import create_app

CSV = "dose,response,group\n1,2.1,a\n2,3.9,a\n3,6.2,a\n4,8.1,b\n5,9.8,b\n6,12.2,b\n"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RESEARCHOS_LATEX_ENGINE", "off")
    app = create_app(ctxflow=CtxFlow())
    with TestClient(app) as test_client:
        yield test_client


def upload(client, text=CSV, project="default", name="trial"):
    return client.post(
        "/api/datasets/upload",
        data={"project_id": project, "name": name},
        files={"file": ("trial.csv", text.encode(), "text/csv")},
    )


def test_engine_skills_are_registered_and_capabilities_are_honest(client):
    names = {s["name"] for s in client.get("/api/skills").json()["skills"]}
    assert {"dataset_ingest", "dataset_analyze", "create_chart", "document_compose",
            "latex_compile", "proof_check", "academic_ocr", "dataset_transform"} <= names

    caps = client.get("/api/workstation/capabilities").json()
    assert caps["artifacts"]["formats"]["pdf"] is False
    assert "linear_regression" in caps["analytics"]["operations"]


def test_dataset_to_report_flow(client):
    dataset = upload(client).json()
    assert dataset["version"] == 1 and dataset["row_count"] == 6

    preview = client.get("/api/datasets/trial/preview", params={"limit": 2}).json()
    assert preview["total_rows"] == 6 and len(preview["rows"]) == 2

    analysis = client.post("/api/analyses", json={
        "dataset_id": "trial", "operation": "linear_regression", "params": {"y": "response", "x": ["dose"]},
    }).json()
    assert analysis["values"]["r_squared"] > 0.99

    chart = client.post("/api/charts", json={"spec": {
        "chart_type": "scatter", "dataset_id": "trial", "x": "dose", "y": "response", "series": "group"}}).json()
    image = client.get(f"/api/charts/{chart['chart_id']}/image")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")

    client.post("/api/papers/ingest", json={
        "title": "Dose response in trials", "abstract": "Response increases linearly with dose in trial cohorts.",
        "authors": ["A. Researcher"], "year": 2024})
    report = client.post("/api/artifacts/compose", json={
        "topic": "dose response", "analysis_ids": [analysis["analysis_id"]], "chart_ids": [chart["chart_id"]],
    }).json()
    assert report["status"] == "partial"
    assert report["builds"][-1]["outputs"]["pdf"]["status"] == "unavailable"

    markdown = client.get(f"/api/artifacts/{report['artifact_id']}/files/markdown")
    assert markdown.status_code == 200 and "trial v1" in markdown.text
    html = client.get(f"/api/artifacts/{report['artifact_id']}/files/html")
    assert "attachment" in html.headers["content-disposition"]

    assert client.get(f"/api/artifacts/{report['artifact_id']}/staleness").json()["stale"] is False
    upload(client, CSV.replace("12.2", "13.0"))
    stale = client.get(f"/api/artifacts/{report['artifact_id']}/staleness").json()
    assert stale["stale"] is True

    rebuilt = client.post(f"/api/artifacts/{report['artifact_id']}/build", json={"refresh_inputs": True}).json()
    assert rebuilt["latest_version"] == 2 and rebuilt["builds"][-1]["refreshed"]
    assert any(s["title"] == "Dose response in trials" for s in client.get("/api/sources").json()["sources"])


def test_validation_errors_and_missing_resources_map_to_4xx(client):
    upload(client)
    bad = client.post("/api/analyses", json={"dataset_id": "trial", "operation": "shell", "params": {}})
    assert bad.status_code == 400 and "Unknown operation" in bad.json()["detail"]
    missing = client.post("/api/analyses", json={"dataset_id": "nope", "operation": "describe"})
    assert missing.status_code == 404
    assert client.get("/api/artifacts/doc-missing/files/markdown").status_code == 404
    assert client.get("/api/datasets", params={"project_id": "../etc"}).status_code == 400
    unresolved = client.post("/api/artifacts", json={"spec": {"title": "x", "sections": [
        {"heading": "s", "blocks": [{"type": "paragraph", "text": "[@ghost]"}]}]}}).json()
    assert unresolved["status"] == "error"


def test_datasets_do_not_leak_across_projects(client):
    client.post("/api/projects", json={"project_id": "alpha"})
    client.post("/api/projects", json={"project_id": "beta"})
    upload(client, project="alpha", name="secret")
    assert client.get("/api/datasets", params={"project_id": "beta"}).json()["total"] == 0
    leaked = client.post("/api/analyses", json={"project_id": "beta", "dataset_id": "secret", "operation": "describe"})
    assert leaked.status_code == 404


def test_proof_endpoint_rejects_code_and_document_upload_requires_pdf(client):
    rejected = client.post("/api/proofs/check", json={"source": "#eval IO.println \"x\""}).json()
    assert rejected["status"] == "rejected"
    upload_response = client.post("/api/documents/upload", data={"project_id": "default"},
                                  files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert upload_response.status_code == 400
