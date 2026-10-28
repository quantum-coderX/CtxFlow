"""Tests for the workspace UI, notes API, and routing agents."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from researchos.api.server import create_app


@pytest.fixture
def client():
    with TestClient(create_app(ctxflow=CtxFlow())) as test_client:
        yield test_client


class TestWorkspaceUI:
    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "ResearchOS" in response.text
        # The bundled UI is either the legacy vanilla bundle or the Vite build.
        assert "/app.js" in response.text or "/assets/" in response.text

    def test_static_assets_are_served(self, client):
        import re

        html = client.get("/").text
        match = re.search(r'(?:src|href)="(/assets/[^"]+\.(?:js|css))"', html)
        if match:
            assert client.get(match.group(1)).status_code == 200
        else:
            assert client.get("/app.js").status_code == 200
            assert client.get("/styles.css").status_code == 200


class TestNotes:
    def test_create_and_list_notes(self, client):
        other_revision_before = client.get(
            "/api/graph/stats?project_id=other"
        ).json()["revision"]
        created = client.post(
            "/api/notes",
            json={
                "title": "Leakage hypothesis",
                "content": "The 85% accuracy may be inflated by test leakage.",
                "project_id": "thesis",
                "tags": ["accuracy"],
            },
        )
        assert created.status_code == 200
        note = created.json()
        assert note["node_id"]
        assert note["project_id"] == "thesis"
        assert "note" in note["tags"]

        listed = client.get("/api/notes?project_id=thesis")
        assert listed.status_code == 200
        data = listed.json()
        assert data["total"] == 1
        assert data["notes"][0]["title"] == "Leakage hypothesis"
        graph = client.get("/api/graph/data?project_id=thesis").json()
        assert any(node["id"] == note["node_id"] for node in graph["nodes"])
        revision = client.get("/api/graph/stats?project_id=thesis").json()["revision"]
        assert revision > 0

        # Project isolation.
        other = client.get("/api/notes?project_id=other").json()
        assert other["total"] == 0
        other_graph = client.get("/api/graph/stats?project_id=other").json()
        assert other_graph["revision"] == other_revision_before


class TestRoutingAgents:
    def test_exploratory_trace_route_returns_provenance(self, client):
        ingest = client.post(
            "/api/papers/ingest",
            json={
                "title": "Graph Memory for Agents",
                "abstract": (
                    "Graph memory retains provenance links between claims "
                    "and their source papers."
                ),
            },
        ).json()

        response = client.post(
            "/api/chat",
            json={"query": "Trace the provenance and lineage of this claim"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["intent"] == "exploratory_trace"
        assert "provenance" in data["answer"].lower()
        assert ingest["paper_node_id"]

    def test_claim_verification_route_returns_verdict_text(self, client):
        client.post(
            "/api/papers/ingest",
            json={
                "title": "LoRA",
                "abstract": (
                    "LoRA freezes pretrained weights and injects trainable "
                    "rank decomposition matrices."
                ),
            },
        )

        response = client.post(
            "/api/chat",
            json={"query": "Verify the claim about LoRA rank decomposition"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["intent"] == "claim_verification"
        assert data["answer"]


class TestPaperIngestMetadata:
    def test_project_and_contradiction_fields(self, client):
        response = client.post(
            "/api/papers/ingest",
            json={
                "project_id": "thesis",
                "title": "Scaling Laws",
                "abstract": (
                    "Language model loss scales as a power law with compute."
                ),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == "thesis"
        assert data["contradictions_found"] == 0
