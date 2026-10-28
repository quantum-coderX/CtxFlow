"""Tests for per-request context budgeting and the demo corpus seed."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from researchos.agents.synthesizer import SynthesizerAgent
from researchos.api.server import create_app
from researchos.demo import load_corpus, seed_demo_corpus


def _client() -> TestClient:
    return TestClient(create_app(ctxflow=CtxFlow()))


class TestDemoCorpus:
    def test_seed_is_idempotent(self):
        with _client() as client:
            corpus_size = len(load_corpus())
            first = client.post("/api/demo/seed", json={"project_id": "demo"})
            assert first.status_code == 200
            first_body = first.json()
            assert first_body["papers_ingested"] == corpus_size
            assert first_body["claims_ingested"] > 0
            assert first_body["nodes"] > first_body["papers_ingested"]
            assert first_body["relations_applied"] >= 2

            second = client.post("/api/demo/seed", json={"project_id": "demo"})
            assert second.status_code == 200
            assert second.json()["papers_ingested"] == 0
            assert second.json()["papers_skipped"] == corpus_size

    def test_seed_helper_uses_real_corpus_metadata(self):
        ctxflow = CtxFlow()
        papers = load_corpus()[:2]
        stats = seed_demo_corpus(ctxflow, project_id="demo", papers=papers)

        assert stats["papers_ingested"] == 2
        titles = {
            node.metadata.get("title")
            for node in ctxflow.graph.all_nodes()
            if node.node_type == "paper"
        }
        assert titles == {paper["title"] for paper in papers}

    def test_corpus_is_real_arxiv_metadata_only(self):
        papers = load_corpus()
        assert len(papers) == 22
        assert all(paper.get("arxiv_id") for paper in papers)
        assert not any(paper.get("synthetic") for paper in papers)

    def test_curated_relations_surface_in_trace(self):
        from ctxflow.models import NodeStatus

        ctxflow = CtxFlow()
        stats = seed_demo_corpus(ctxflow, project_id="demo")

        assert stats["relations_applied"] == 2
        assert stats["superseded_claims"] == 1
        assert stats["contested_claims"] == 1

        supersedes = [
            edge
            for node in ctxflow.graph.all_nodes()
            for edge in ctxflow.graph.get_edges_from(node.id)
            if edge.relation_type == "supersedes"
        ]
        assert len(supersedes) == 1
        edge = supersedes[0]

        # Tracing the *new* claim must surface the older superseded claim even
        # though it is not an ancestor in the backward provenance chain.
        trace = ctxflow.research_trace(edge.source_id)
        assert edge.target_id in {node.id for node in trace.superseded_claims}
        assert ctxflow.graph.get_node(edge.target_id).status == NodeStatus.SUPERSEDED


class TestContextBudget:
    def test_request_budget_controls_packed_nodes(self):
        with _client() as client:
            client.post("/api/demo/seed", json={"project_id": "demo"})
            query = "How does GraphRAG compare with flat vector retrieval?"

            small = client.post(
                "/api/chat",
                json={"query": query, "token_budget": 400},
            )
            large = client.post(
                "/api/chat",
                json={"query": query, "token_budget": 6000},
            )

        assert small.status_code == 200
        assert large.status_code == 200

        small_usage = small.json()["metadata"]["context_usage"]
        large_usage = large.json()["metadata"]["context_usage"]

        assert small_usage["token_budget"] == 400
        assert large_usage["token_budget"] == 6000
        assert small_usage["nodes_used"] < large_usage["nodes_used"]
        assert small_usage["nodes_dropped"] > 0
        assert small_usage["tokens_used"] <= 400
        assert len(small.json()["cited_nodes"]) == small_usage["nodes_used"]
        assert len(large.json()["cited_nodes"]) == large_usage["nodes_used"]

    def test_synthesizer_falls_back_to_default_for_invalid_budget(self):
        ctxflow = CtxFlow()
        ctxflow.ingest("Graph memory stores claims as linked nodes.", tags={"graph"})
        synthesizer = SynthesizerAgent(ctxflow, token_budget=1024)

        result = synthesizer.run(
            query="graph memory",
            context={"token_budget": "not-a-number"},
        )

        assert result.metadata["token_budget"] == 1024
