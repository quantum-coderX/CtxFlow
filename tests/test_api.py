"""Integration and unit tests for ResearchOS FastAPI Endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from ctxflow.models import CtxFlowConfig
from researchos.api.server import create_app


@pytest.fixture
def test_client():
    cf = CtxFlow()
    app = create_app(ctxflow=cf)
    with TestClient(app) as client:
        yield client


class TestApiEndpoints:
    """Tests for ResearchOS REST and system endpoints."""

    def test_health_endpoint(self, test_client):
        res = test_client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert "graph_nodes" in data

    def test_list_skills(self, test_client):
        res = test_client.get("/api/skills")
        assert res.status_code == 200
        skills = res.json()["skills"]
        skill_names = [s["name"] for s in skills]
        assert "graph_query" in skill_names
        assert "literature_search" in skill_names
        assert "review_writer" in skill_names

    def test_paper_ingest_and_graph_data(self, test_client):
        revision_before = test_client.get("/api/graph/stats").json()["revision"]

        # 1. Ingest paper
        ingest_payload = {
            "title": "Scaling Laws for Neural Language Models",
            "authors": ["Jared Kaplan", "Sam McCandlish"],
            "year": 2020,
            "abstract": "We observe that language model performance scales as a power-law with compute, dataset size, and parameter count.",
        }
        res = test_client.post("/api/papers/ingest", json=ingest_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["paper_node_id"] != ""
        assert len(data["claim_node_ids"]) >= 1

        # 2. Check stats
        stats_res = test_client.get("/api/graph/stats")
        assert stats_res.status_code == 200
        stats = stats_res.json()
        assert stats["total_nodes"] >= 2
        assert stats["total_edges"] >= 1
        assert stats["revision"] > revision_before

        # 3. Check graph data
        graph_res = test_client.get("/api/graph/data")
        assert graph_res.status_code == 200
        g_data = graph_res.json()
        assert len(g_data["nodes"]) >= 2
        assert len(g_data["edges"]) >= 1

    def test_chat_query_flow(self, test_client):
        # Ingest knowledge first
        test_client.post(
            "/api/papers/ingest",
            json={
                "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                "abstract": "BERT is designed to pre-train deep bidirectional representations from unlabeled text.",
            },
        )

        chat_payload = {
            "query": "Explain what BERT does",
            "include_trace": True,
        }
        res = test_client.post("/api/chat", json=chat_payload)
        assert res.status_code == 200
        chat_data = res.json()
        assert "answer" in chat_data
        assert chat_data["intent"] != ""

    def test_trace_endpoint(self, test_client):
        ingest_res = test_client.post(
            "/api/papers/ingest",
            json={
                "title": "FlashAttention: Fast and Memory-Efficient Exact Attention",
                "abstract": "FlashAttention reduces memory accesses between GPU high bandwidth memory and on-chip SRAM.",
            },
        )
        claim_id = ingest_res.json()["claim_node_ids"][0]

        trace_res = test_client.post("/api/trace", json={"node_id": claim_id})
        assert trace_res.status_code == 200
        t_data = trace_res.json()
        assert t_data["answer_node_id"] == claim_id
        assert len(t_data["source_papers"]) >= 1
