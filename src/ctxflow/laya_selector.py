"""Context selection via Laya System-1 decision model.

Replaces greedy token-packing with learned judgments about which memories
are relevant to the current task, tools, and session state.

Laya is a non-autoregressive model trained with RLCD to return calibrated
probabilities. It runs in a single forward pass (~33 ms) and never generates
text, so there's no hallucination risk.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from ctxflow.memory.store import Memory, MemoryStore, ValidityState
from ctxflow.memory.tokens import SimpleTokenizer, count_tokens

_LOGGER = logging.getLogger(__name__)

# Validity states that deterministic code always excludes, whatever Laya says.
_HARD_INVALID = frozenset({ValidityState.SUPERSEDED, ValidityState.CONTRADICTED})


# ---------------------------------------------------------------------------
# Decision protocol (what Laya will score)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryRelevanceQuestion:
    """Typed question for Laya to score a candidate memory."""

    memory_id: str
    text: str
    memory_type: str
    recency_percentile: float  # 0-1, where 1 = most recent
    access_count: int
    importance_score: float  # 0-1
    validity: str  # "valid", "stale", "superseded", etc.


@dataclass(frozen=True)
class LayaDecision:
    """Laya's judgment on whether to include a memory in context."""

    memory_id: str
    keep: bool
    relevance_score: float  # 0-1, calibrated confidence
    reason: str  # "task_match" | "dependency" | "continuity" | "edge_case" | "low_confidence"


# ---------------------------------------------------------------------------
# Laya Router adapter (from laya library)
# ---------------------------------------------------------------------------

class LayaRouter(Protocol):
    """Protocol for Laya's Router class (from `laya` package)."""

    def predict(
        self,
        state: str,
        questions: dict[str, Any],
        model: str = "english",
        max_len: int = 1024,
    ) -> dict[str, Any]:
        """
        Call Laya's predict method.

        Parameters
        ----------
        state : str
            Context string (the memories + query + task description).
        questions : dict
            Typed questions for Laya to answer (choice, score, etc.).
        model : str
            Which checkpoint ("english", "multilingual", etc.).
        max_len : int
            Maximum input length (default 1024, up to 8192 for long documents).

        Returns
        -------
        dict
            Response keys match the question keys; each value is a dict
            with "answer" and "confidence" (0-1, calibrated).
        """
        ...


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or the to what which "
    "with this that find recent papers paper about general query task".split()
)


def _terms(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS
    }


def _state_field(state: str, name: str) -> str:
    match = re.search(rf"^{name}: (.*)$", state, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


class HeuristicLayaRouter:
    """Deterministic stand-in for ``laya.Router`` when the package is absent.

    Answers the same typed questions from the structured state string built by
    :class:`LayaContextSelector`, so the selection pipeline (and its feature
    flag) works without the learned checkpoint. Lexical overlap is a weak
    proxy for task relevance; install ``laya`` for learned decisions.
    """

    name = "heuristic"

    def predict(
        self,
        state: str,
        questions: dict[str, Any],
        model: str = "english",
        max_len: int = 1024,
    ) -> dict[str, Any]:
        text = state.split("\nTEXT:\n", 1)[1] if "\nTEXT:\n" in state else state
        task_terms = _terms(
            " ".join(
                _state_field(state, key) for key in ("QUERY", "TASK", "SKILLS")
            )
        )
        overlap = len(task_terms & _terms(text)) / max(1, len(task_terms))
        if overlap >= 0.25:
            task_match = "yes"
        elif overlap > 0:
            task_match = "maybe"
        else:
            task_match = "no"

        validity = _state_field(state, "VALIDITY")
        if validity == ValidityState.VALID.value:
            validity_answer = "valid"
        elif validity in {ValidityState.STALE.value, ValidityState.REQUIRES_REVIEW.value}:
            validity_answer = "uncertain"
        else:
            validity_answer = "invalid"

        continuity = 2 if _state_field(state, "IN_PREVIOUS_CONTEXT") == "yes" else 0
        try:
            importance_value = float(_state_field(state, "IMPORTANCE") or 0.0)
        except ValueError:
            importance_value = 0.0
        importance = 2 if importance_value >= 0.7 else (1 if importance_value >= 0.3 else 0)

        return {
            "task_match": {"answer": task_match, "confidence": min(1.0, 0.5 + overlap / 2)},
            "validity_check": {"answer": validity_answer, "confidence": 0.9},
            "continuity": {"answer": continuity, "confidence": 0.9},
            "importance": {"answer": importance, "confidence": 0.7},
        }


def default_router() -> tuple[LayaRouter, str]:
    """``laya.Router`` when installed, otherwise the deterministic heuristic."""
    try:
        from laya import Router  # type: ignore[import-not-found]
    except ImportError:
        _LOGGER.info(
            "laya package not installed; using HeuristicLayaRouter "
            "(pip install laya for learned decisions)"
        )
        return HeuristicLayaRouter(), HeuristicLayaRouter.name
    return Router(), "laya"


def _score_answer(value: Any) -> float:
    """Clamp a 0-2 Laya score answer, tolerating strings and junk."""
    try:
        return max(0.0, min(2.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Laya-aware context selector
# ---------------------------------------------------------------------------

class LayaContextSelector:
    """Uses Laya to decide which memories belong in the context.

    Instead of greedy token-packing (take highest-scored memories until
    budget is exhausted), this model reasons about:
    - Task dependencies (what tools/skills are involved?)
    - Previous context (what was just mentioned?)
    - Validity (is this memory stale or superseded?)
    - Diversity (avoid duplicate/near-duplicate memories)
    """

    def __init__(
        self,
        store: MemoryStore,
        router: LayaRouter | None = None,
        tokenizer: Any | None = None,
        model_name: str = "english",
        max_input_tokens: int = 1024,
        candidate_limit: int = 200,
    ) -> None:
        """
        Parameters
        ----------
        store : MemoryStore
            Async memory storage for retrieval.
        router : LayaRouter | None
            Laya Router instance. If None, ``laya.Router`` is used when the
            package is installed, otherwise :class:`HeuristicLayaRouter`.
        tokenizer : TokenizerAdapter | None
            Token counter. Defaults to SimpleTokenizer.
        model_name : str
            Which Laya checkpoint ("english", "multilingual", etc.).
        max_input_tokens : int
            Maximum tokens to send to Laya (1024 or 8192).
        candidate_limit : int
            Memories over-retrieved by hybrid search before Laya filters them.
        """
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        self.store = store
        self.tokenizer = tokenizer or SimpleTokenizer()
        self.model_name = model_name
        self.max_input_tokens = max_input_tokens
        self.candidate_limit = candidate_limit

        self.router, self.router_name = (
            default_router() if router is None
            else (router, str(getattr(router, "name", "laya")))
        )

    async def select_context(
        self,
        project_id: str,
        query: str,
        task_description: str,
        required_skills: list[str] | None = None,
        previous_context_ids: list[str] | None = None,
        token_budget: int = 2048,
    ) -> list[dict[str, Any]]:
        """
        Select high-value memories for the LLM context using Laya.

        Parameters
        ----------
        project_id : str
            Project scope for retrieval.
        query : str
            User query or task.
        task_description : str
            High-level description of what the LLM is trying to do
            (e.g., "tool_call: arxiv_search", "reasoning: fact_check",
            "summary: research paper").
        required_skills : list[str] | None
            Skills/tools the LLM might call (e.g., ["web_search", "code_gen"]).
        previous_context_ids : list[str] | None
            Memory IDs from the previous turn (for continuity).
        token_budget : int
            Maximum tokens to allocate to context.

        Returns
        -------
        list[dict]
            Selected ContextRecords (memory_id, text, tokens, score, reason).
        """
        required_skills = required_skills or []
        previous_context_ids = previous_context_ids or []

        # Phase 1: Retrieve candidate memories via hybrid search.
        candidates = await self.store.search(
            None,
            query,
            self.candidate_limit,  # Over-retrieve; Laya will filter.
            project_id=project_id,
            include_archived=False,
            include_history=False,
        )

        if not candidates:
            _LOGGER.debug("No memories retrieved for query %r", query)
            return []

        # Phase 2: Load full memory objects.
        memories = []
        for scored in candidates:
            mem = await self.store.get(
                scored.memory_id, project_id=project_id
            )
            if mem is not None:
                memories.append(mem)

        if not memories:
            return []

        # Phase 3: Compute recency percentiles and prepare questions for Laya.
        now_ts = max(m.created_at.timestamp() for m in memories)
        min_ts = min(m.created_at.timestamp() for m in memories)
        time_range = max(now_ts - min_ts, 1.0)

        questions = self._build_laya_questions(
            memories,
            query,
            task_description,
            required_skills,
            previous_context_ids,
            now_ts,
            time_range,
        )

        # Phase 4: Call Laya to decide keep/drop for each memory.
        decisions = self._call_laya(
            memories,
            questions,
            task_description,
            required_skills,
            query=query,
            previous_context_ids=previous_context_ids,
        )

        # Phase 5: Sort by Laya's relevance scores (highest first).
        ranked = sorted(
            zip(memories, decisions),
            key=lambda x: x[1].relevance_score,
            reverse=True,
        )

        # Phase 6: Pack within token budget, respecting Laya's keep decisions.
        selected = []
        used_tokens = 0

        for memory, decision in ranked:
            if not decision.keep:
                _LOGGER.debug(
                    "Laya dropped memory %s: %s (confidence=%.2f)",
                    decision.memory_id,
                    decision.reason,
                    decision.relevance_score,
                )
                continue

            tokens = count_tokens(memory, self.tokenizer)
            if used_tokens + tokens > token_budget:
                _LOGGER.debug(
                    "Skipping memory %s (%d tokens); %d / %d tokens used",
                    memory.id,
                    tokens,
                    used_tokens,
                    token_budget,
                )
                continue

            # Fetch provenance if available.
            source_type = None
            source_id = None
            if hasattr(self.store, "get_provenance"):
                prov = await self.store.get_provenance(
                    memory.id, project_id=project_id
                )
                if prov is not None:
                    source_type = prov.source_type.value
                    source_id = prov.source_id

            selected.append({
                "memory_id": memory.id,
                "text": memory.text,
                "tokens": tokens,
                "memory_type": memory.memory_type.value,
                "validity": memory.validity.value,
                "importance": memory.importance,
                "score": decision.relevance_score,
                "reason": decision.reason,
                "source_type": source_type,
                "source_id": source_id,
            })
            used_tokens += tokens

        _LOGGER.info(
            "Laya selected %d/%d memories (%d tokens used)",
            len(selected),
            len(memories),
            used_tokens,
        )
        return selected

    # -- Private helpers ----------------------------------------------------

    def _build_laya_questions(
        self,
        memories: list[Memory],
        query: str,
        task_description: str,
        required_skills: list[str],
        previous_context_ids: list[str],
        now_ts: float,
        time_range: float,
    ) -> dict[str, Any]:
        """Build the typed questions dict for Laya's predict method."""
        
        previous_set = set(previous_context_ids)
        
        # For each memory, we'll ask Laya a set of scored questions.
        # Laya will answer each question (yes/no or score) with a confidence.
        
        return {
            "task_match": {
                "type": "choice",
                "instructions": (
                    "Does this memory help with the task? "
                    f"(Task: {task_description})"
                ),
                "criteria": {
                    "yes": "Directly relevant to the task or required skill",
                    "maybe": "Tangentially related; could be useful context",
                    "no": "Unrelated to the task",
                },
            },
            "validity_check": {
                "type": "choice",
                "instructions": "Is this memory still valid and not superseded?",
                "criteria": {
                    "valid": "Current and reliable",
                    "uncertain": "Possibly stale but might still apply",
                    "invalid": "Stale, contradicted, or superseded",
                },
            },
            "continuity": {
                "type": "score",
                "instructions": "Does this memory connect to recent context?",
                "criteria": [
                    "Not connected to previous turn",
                    "Loosely connected",
                    "Directly referenced or depends on recent context",
                ],
            },
            "importance": {
                "type": "score",
                "instructions": "How important is this memory for understanding?",
                "criteria": [
                    "Background or low-importance detail",
                    "Supporting information",
                    "Critical fact or decision",
                ],
            },
        }

    def _call_laya(
        self,
        memories: list[Memory],
        questions: dict[str, Any],
        task_description: str,
        required_skills: list[str],
        *,
        query: str = "",
        previous_context_ids: list[str] | None = None,
    ) -> list[LayaDecision]:
        """Call Laya's predict for each memory and collect decisions."""
        
        decisions = []
        skills_str = ", ".join(required_skills) if required_skills else "general"
        previous_set = set(previous_context_ids or [])

        for memory in memories:
            if memory.validity in _HARD_INVALID:
                decisions.append(
                    LayaDecision(
                        memory_id=memory.id,
                        keep=False,
                        relevance_score=0.0,
                        reason="stale_or_superseded",
                    )
                )
                continue

            # Construct a brief state string with memory context.
            state = (
                f"QUERY: {query}\n"
                f"TASK: {task_description}\n"
                f"SKILLS: {skills_str}\n"
                f"MEMORY TYPE: {memory.memory_type.value}\n"
                f"VALIDITY: {memory.validity.value}\n"
                f"IMPORTANCE: {memory.importance:.2f}\n"
                f"ACCESS COUNT: {memory.access_count}\n"
                f"IN_PREVIOUS_CONTEXT: {'yes' if memory.id in previous_set else 'no'}\n"
                f"\nTEXT:\n{memory.text}"
            )

            try:
                result = self.router.predict(
                    state,
                    questions,
                    model=self.model_name,
                    max_len=self.max_input_tokens,
                )

                # Extract Laya's answers.
                task_match = result.get("task_match", {}).get("answer", "no")
                validity_check = result.get("validity_check", {}).get("answer", "invalid")
                continuity = _score_answer(result.get("continuity", {}).get("answer", 0))
                importance = _score_answer(result.get("importance", {}).get("answer", 0))

                # Map answers to a keep/drop decision.
                keep = (
                    task_match in {"yes", "maybe"}
                    and validity_check in {"valid", "uncertain"}
                )

                # Compute a combined relevance score (0-1).
                # Weight: task_match (40%), validity (30%), continuity (15%), importance (15%).
                task_weight = 1.0 if task_match == "yes" else (0.5 if task_match == "maybe" else 0.0)
                validity_weight = 1.0 if validity_check == "valid" else (0.5 if validity_check == "uncertain" else 0.0)
                continuity_weight = continuity / 2.0  # Score is 0-2, normalize to 0-1
                importance_weight = importance / 2.0

                relevance_score = (
                    0.40 * task_weight
                    + 0.30 * validity_weight
                    + 0.15 * continuity_weight
                    + 0.15 * importance_weight
                )

                # Determine reason for keep/drop.
                if not keep:
                    reason = (
                        "low_relevance"
                        if task_match == "no"
                        else "stale_or_superseded"
                    )
                elif task_match == "yes":
                    reason = "task_match"
                elif continuity >= 1:
                    reason = "continuity"
                elif importance >= 1:
                    reason = "importance"
                else:
                    reason = "edge_case"

                decisions.append(
                    LayaDecision(
                        memory_id=memory.id,
                        keep=keep,
                        relevance_score=max(0.0, min(1.0, relevance_score)),
                        reason=reason,
                    )
                )
            except Exception as e:
                _LOGGER.warning(
                    "Laya decision failed for memory %s: %s; dropping",
                    memory.id,
                    e,
                )
                decisions.append(
                    LayaDecision(
                        memory_id=memory.id,
                        keep=False,
                        relevance_score=0.0,
                        reason="decision_error",
                    )
                )

        return decisions
