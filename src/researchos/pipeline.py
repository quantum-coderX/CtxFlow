"""Compose a SkillRouter bound to one project graph."""

from __future__ import annotations

from typing import Any, Callable, Optional

from ctxflow import CtxFlow
from researchos.agents.critic import CriticAgent
from researchos.agents.reader import ReaderAgent
from researchos.agents.retriever import RetrieverAgent
from researchos.agents.router import SkillRouter
from researchos.agents.synthesizer import SynthesizerAgent
from researchos.agents.trace import TraceAgent
from researchos.skills.literature_search import LiteratureSearchSkill
from researchos.skills.paper_reader import PaperReaderSkill

LLMCallable = Callable[[str, str], str]


def make_router(
    ctxflow: CtxFlow,
    *,
    literature_search: Optional[LiteratureSearchSkill] = None,
    paper_reader: Optional[PaperReaderSkill] = None,
    llm: Optional[LLMCallable] = None,
    token_budget: int = 2048,
) -> SkillRouter:
    """Build the four-agent pipeline against *ctxflow*."""
    retriever = RetrieverAgent(
        ctxflow=ctxflow, literature_search=literature_search, llm=llm
    )
    reader = ReaderAgent(ctxflow=ctxflow, paper_reader=paper_reader, llm=llm)
    synthesizer = SynthesizerAgent(
        ctxflow=ctxflow, llm=llm, token_budget=token_budget
    )
    critic = CriticAgent(ctxflow=ctxflow, llm=llm)
    tracer = TraceAgent(ctxflow=ctxflow, llm=llm)
    return SkillRouter(
        agents={
            "retriever": retriever,
            "reader": reader,
            "synthesizer": synthesizer,
            "critic": critic,
            "graph_query": tracer,
        },
        llm=llm,
    )


def redact_secrets(payload: Any) -> Any:
    """Drop credential-like keys from nested dicts before returning to clients."""
    secret_keys = {
        "api_key",
        "apikey",
        "authorization",
        "token",
        "password",
        "secret",
        "groq_api_key",
    }
    if isinstance(payload, dict):
        return {
            key: "[redacted]" if str(key).lower() in secret_keys else redact_secrets(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_secrets(item) for item in payload]
    return payload
