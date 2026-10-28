"""Unit tests for ResearchOS Multi-Agent Pipeline."""

from __future__ import annotations

import time
from typing import List

import pytest

from ctxflow import CtxFlow
from ctxflow.models import CtxFlowConfig, Edge, NodeStatus
from researchos.agents.base import AgentResult, BaseAgent
from researchos.agents.critic import CriticAgent
from researchos.agents.reader import ReaderAgent
from researchos.agents.retriever import RetrieverAgent
from researchos.agents.router import QueryIntent, SkillRouter, classify_intent
from researchos.agents.synthesizer import SynthesizerAgent
from researchos.skills.paper_reader import PaperReaderSkill, PaperSection


class TestSkillRouter:
    """Tests for SkillRouter intent classification and dispatch."""

    def test_classify_intent(self):
        assert classify_intent("Find recent papers on diffusion models") == QueryIntent.NEW_LITERATURE
        assert classify_intent("Verify if the claim about accuracy is true") == QueryIntent.CLAIM_VERIFICATION
        assert classify_intent("Trace provenance and lineage of this claim") == QueryIntent.EXPLORATORY_TRACE
        assert classify_intent("Explain what is known about attention mechanisms") == QueryIntent.EXISTING_KNOWLEDGE

    def test_router_execution_flow(self):
        # Dummy agent
        class EchoAgent(BaseAgent):
            def execute(self, **kwargs):
                return AgentResult(agent_name=self.name, success=True, data={"echo": kwargs.get("query")})

        router = SkillRouter(
            agents={
                "synthesizer": EchoAgent("synthesizer"),
                "critic": EchoAgent("critic"),
            }
        )

        res = router.run(query="What is the architecture of ResNet?")
        assert res.success is True
        assert res.data["intent"] == QueryIntent.EXISTING_KNOWLEDGE.value
        assert len(res.data["pipeline_results"]) == 2


class TestRetrieverAgent:
    """Tests for RetrieverAgent graph search and gap detection."""

    def test_retriever_internal_only(self):
        cf = CtxFlow()
        cf.ingest("Graph neural networks process topological graph data.", node_type="claim", tags={"gnn", "graph"})
        cf.ingest("Message passing algorithms aggregate neighbor representations.", node_type="claim", tags={"gnn", "graph"})

        retriever = RetrieverAgent(ctxflow=cf, literature_search=None, min_internal_results=1, min_score_threshold=0.1)
        res = retriever.run(query="graph neural networks")

        assert res.success is True
        assert len(res.data["internal_results"]) >= 1


class TestReaderAgent:
    """Tests for ReaderAgent paper and claim ingestion."""

    def test_reader_ingestion(self):
        cf = CtxFlow()
        reader = ReaderAgent(ctxflow=cf, llm=None)

        sample_paper = {
            "title": "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
            "authors": ["Albert Gu", "Tri Dao"],
            "year": 2023,
            "abstract": "We introduce Mamba, a fast architecture. It achieves 5x higher throughput than Transformers.",
        }

        res = reader.run(
            query="mamba",
            context={"previous_output": {"external_results": [sample_paper]}},
        )

        assert res.success is True
        assert res.data["total_nodes_created"] >= 2
        assert cf.graph.node_count >= 2

    def test_claim_extraction_can_skip_the_model(self, monkeypatch):
        """Token-capped tiers keep the model for synthesis, not extraction."""
        monkeypatch.setenv("RESEARCHOS_LLM_CLAIM_EXTRACTION", "0")

        prompts: List[str] = []

        def llm(prompt, system=""):
            prompts.append(prompt)
            return "[]"

        cf = CtxFlow()
        reader = ReaderAgent(ctxflow=cf, llm=llm)
        claims = reader._extract_abstract_claims(
            "We introduce Mamba, a fast architecture. "
            "It achieves 5x higher throughput than Transformers."
        )

        assert prompts == []
        assert len(claims) == 2
        assert "Mamba" in claims[0]["text"]

    def test_fulltext_top3_is_opt_in_and_bounded_to_first_three(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_FULLTEXT", "top3")

        class FakePaperReader:
            def __init__(self):
                self.paths = []

            def read_selected_sections(self, path):
                self.paths.append(path)
                return {
                    "content": "Introduction excerpt",
                    "claims": [{
                        "type": "claim",
                        "text": "A full-text claim with enough words to be useful.",
                        "section": "Introduction",
                        "page": 2,
                    }],
                }

        reader_skill = FakePaperReader()
        reader = ReaderAgent(ctxflow=CtxFlow(), paper_reader=reader_skill)
        downloaded = []

        def fake_download(arxiv_id, remaining_s):
            downloaded.append(arxiv_id)
            assert remaining_s > 0
            return b"%PDF-fake"

        monkeypatch.setattr(reader, "_download_arxiv_pdf", fake_download)
        ingested = []
        monkeypatch.setattr(
            reader,
            "_ingest_paper_detail",
            lambda paper, query: (ingested.append(paper) or [paper["arxiv_id"]], False),
        )
        papers = [
            {"title": str(index), "arxiv_id": f"2405.1650{index}"}
            for index in range(4)
        ]

        result = reader.run(
            query="test",
            context={
                "previous_output": {"external_results": papers},
                "fulltext_deadline": time.monotonic() + 10,
            },
        )

        assert result.success
        assert downloaded == [paper["arxiv_id"] for paper in papers[:3]]
        assert len(reader_skill.paths) == 3
        assert [paper.get("access_status") for paper in ingested] == [
            "full_text",
            "full_text",
            "full_text",
            None,
        ]
        assert ingested[0]["claims"][0]["section"] == "Introduction"
        assert ingested[0]["claims"][0]["page"] == 2

    def test_fulltext_defaults_off(self, monkeypatch):
        monkeypatch.delenv("RESEARCHOS_FULLTEXT", raising=False)
        reader = ReaderAgent(ctxflow=CtxFlow())
        monkeypatch.setattr(
            reader,
            "_download_arxiv_pdf",
            lambda *args: pytest.fail("full-text download must be disabled by default"),
        )
        monkeypatch.setattr(reader, "_ingest_paper_detail", lambda *args: ([], False))

        result = reader.run(
            query="test",
            context={
                "previous_output": {
                    "external_results": [{"title": "paper", "arxiv_id": "2405.16506"}]
                }
            },
        )

        assert result.success

    def test_fulltext_skips_download_after_wall_clock_deadline(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_FULLTEXT", "top3")
        reader = ReaderAgent(ctxflow=CtxFlow())
        monkeypatch.setattr(
            reader,
            "_download_arxiv_pdf",
            lambda *args: pytest.fail("download must not start after the deadline"),
        )
        monkeypatch.setattr(reader, "_ingest_paper_detail", lambda *args: ([], False))

        result = reader.run(
            query="test",
            context={
                "previous_output": {
                    "external_results": [{"title": "paper", "arxiv_id": "2405.16506"}]
                },
                "fulltext_deadline": time.monotonic() - 1,
            },
        )

        assert result.success


def test_paper_reader_limits_fulltext_to_intro_results_and_conclusion(monkeypatch):
    skill = PaperReaderSkill()
    monkeypatch.setattr(
        "researchos.skills.paper_reader.extract_sections_from_pdf",
        lambda path: [
            PaperSection("Abstract", "Abstract text is excluded.", [0]),
            PaperSection("1 Introduction", "The introduction explains the method.", [1]),
            PaperSection("Methods", "Methods text is excluded.", [2]),
            PaperSection("4 Results", "The results improve performance.", [3]),
            PaperSection("Conclusion", "We conclude the method works.", [4]),
        ],
    )

    excerpts = skill.read_selected_sections("unused.pdf")

    assert "Abstract text" not in excerpts["content"]
    assert "Methods text" not in excerpts["content"]
    assert all(
        claim["section"] in {"1 Introduction", "4 Results", "Conclusion"}
        for claim in excerpts["claims"]
    )
    assert {claim["page"] for claim in excerpts["claims"]} == {1, 3, 4}

class TestSynthesizerAgent:
    """Tests for SynthesizerAgent bias-aware reordering and prompt packing."""

    def test_synthesizer_heuristic(self):
        cf = CtxFlow()
        cf.ingest("AdamW decouples weight decay from gradient updates.", node_type="claim", tags={"adamw", "optimizer"})

        synthesizer = SynthesizerAgent(ctxflow=cf, llm=None)
        res = synthesizer.run(query="How does AdamW work?")

        assert res.success is True
        assert "AdamW" in res.data["answer"]
        assert len(res.data["cited_nodes"]) >= 1

    def test_synthesizer_uses_bounded_history_for_follow_up_without_graph_evidence(self):
        prompts = []

        def llm(prompt, system=""):
            prompts.append(prompt)
            return "The prior response recommended checking the retrieval cost."

        synthesizer = SynthesizerAgent(ctxflow=CtxFlow(), llm=llm)
        result = synthesizer.run(
            query="What did you recommend checking?",
            context={
                "token_budget": 256,
                "conversation_history": [
                    {
                        "query": "How does the system trade off retrieval quality and cost?",
                        "answer": "The prior response recommended checking the retrieval cost.",
                    }
                ],
            },
        )

        assert result.success is True
        assert "retrieval cost" in prompts[0]
        assert "untrusted" in prompts[0]
        assert result.data["cited_nodes"] == []
        assert result.metadata["tokens_used"] <= result.metadata["token_budget"]


class TestCriticAgent:
    """Tests for CriticAgent contradiction and supersession detection."""

    def test_critic_supersession_warning(self):
        cf = CtxFlow()

        # Node 1: old claim
        n1 = cf.ingest("Claim 1: Accuracy is 70%", node_type="claim", tags={"accuracy"})
        # Node 2: correction that supersedes Node 1
        n2 = cf.ingest("Claim 2: Corrected accuracy is 75%", node_type="claim", tags={"accuracy"})

        cf.graph.add_edge(Edge(source_id=n2, target_id=n1, relation_type="supersedes"))
        old_node = cf.graph.get_node(n1)
        old_node.status = NodeStatus.SUPERSEDED
        old_node.superseded_by = n2

        critic = CriticAgent(ctxflow=cf)
        res = critic.run(
            query="accuracy check",
            context={"previous_output": {"cited_nodes": [n1], "answer": "The accuracy is 70%."}},
        )

        assert res.success is True
        assert res.data["verdict"] == "WARNINGS_FOUND"
        assert len(res.data["supersession_warnings"]) >= 1

    def test_critic_standalone_claim_verification(self):
        cf = CtxFlow()
        n = cf.ingest("LoRA freezes pretrained weights and injects trainable rank decomposition matrices.", tags={"lora"})

        critic = CriticAgent(ctxflow=cf)
        verif = critic.verify_claim(claim_text="LoRA rank decomposition matrices", node_id=n)

        assert verif["confidence"] > 0
        assert verif["trace"]["exists"] is True
