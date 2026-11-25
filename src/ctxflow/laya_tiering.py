"""Laya-assisted storage-tier proposals for paged memory.

Laya only proposes; the deterministic rule tier and its hard limits decide.
A memory the rules place in COLD (archived, superseded, stale) is never moved,
and a working-tier memory is never demoted below HOT.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ctxflow.laya_selector import LayaRouter, _score_answer, default_router
from ctxflow.memory.store import (
    Memory,
    MemoryTier,
    StorageTier,
    _now,
    storage_tier,
)

_LOGGER = logging.getLogger(__name__)

_QUESTIONS: dict[str, Any] = {
    "validity_check": {
        "type": "choice",
        "instructions": "Is this memory still valid and not superseded?",
        "criteria": {
            "valid": "Current and reliable",
            "uncertain": "Possibly stale but might still apply",
            "invalid": "Stale, contradicted, or superseded",
        },
    },
    "importance": {
        "type": "score",
        "instructions": "How important is this memory for long-term understanding?",
        "criteria": [
            "Background or low-importance detail",
            "Supporting information",
            "Critical fact or decision",
        ],
    },
}


@dataclass(frozen=True)
class TierProposal:
    memory_id: str
    rule_tier: StorageTier
    proposed_tier: StorageTier
    reason: str
    router: str


class LayaTierAdvisor:
    """Proposes HOT/WARM/COLD placement from Laya's calibrated answers."""

    def __init__(
        self,
        router: LayaRouter | None = None,
        *,
        model_name: str = "english",
        max_input_tokens: int = 1024,
        min_confidence: float = 0.6,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if router is None:
            self.router, self.router_name = default_router()
        else:
            self.router = router
            self.router_name = str(getattr(router, "name", "laya"))
        self.model_name = model_name
        self.max_input_tokens = max_input_tokens
        self.min_confidence = min_confidence

    def propose(
        self, memories: Sequence[Memory], *, now: datetime | None = None
    ) -> list[TierProposal]:
        current = now or _now()
        return [self._propose_one(memory, current) for memory in memories]

    def _propose_one(self, memory: Memory, now: datetime) -> TierProposal:
        rule = storage_tier(memory, now=now)

        def result(tier: StorageTier, reason: str) -> TierProposal:
            return TierProposal(memory.id, rule, tier, reason, self.router_name)

        if rule is StorageTier.COLD:
            return result(StorageTier.COLD, "rule_cold")

        age_days = max(0.0, (now - memory.created_at).total_seconds() / 86400)
        state = (
            f"MEMORY TYPE: {memory.memory_type.value}\n"
            f"VALIDITY: {memory.validity.value}\n"
            f"IMPORTANCE: {memory.importance:.2f}\n"
            f"ACCESS COUNT: {memory.access_count}\n"
            f"AGE_DAYS: {age_days:.1f}\n"
            f"\nTEXT:\n{memory.text}"
        )
        try:
            answers = self.router.predict(
                state, _QUESTIONS, model=self.model_name, max_len=self.max_input_tokens
            )
            validity = answers.get("validity_check", {})
            importance = answers.get("importance", {})
            confidence = min(
                float(validity.get("confidence", 0.0)),
                float(importance.get("confidence", 0.0)),
            )
            validity_answer = validity.get("answer", "valid")
            importance_score = _score_answer(importance.get("answer", 1))
        except Exception as error:  # noqa: BLE001 — advisory only; fall back to rules
            _LOGGER.warning("Laya tier proposal failed for %s: %s", memory.id, error)
            return result(rule, "laya_error")

        if confidence < self.min_confidence:
            return result(rule, "low_confidence")

        pinned = memory.tier is MemoryTier.WORKING
        if validity_answer == "invalid" and not pinned:
            return result(StorageTier.COLD, "laya_invalid")
        if importance_score >= 2:
            return result(StorageTier.HOT, "laya_important")
        if importance_score < 1 and validity_answer != "valid" and not pinned:
            return result(StorageTier.COLD, "laya_low_value")
        return result(rule, "agree")
