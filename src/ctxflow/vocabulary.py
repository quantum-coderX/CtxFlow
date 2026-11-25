"""Relation and node-type vocabulary for the typed context multigraph.

The graph stays open-vocabulary: unknown relation or node types are accepted.
The registry only classifies the known ones so traversal code can ask for a
*category* (e.g. provenance) instead of hard-coding relation names.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional


class RelationCategory(str, Enum):
    PROVENANCE = "provenance"  # walked backward to find sources
    CONTRADICTION = "contradiction"  # supersession / conflict between claims
    STRUCTURAL = "structural"  # dependency, summary, duplication, similarity
    RESEARCH = "research"  # literature and evidence relations


@dataclass(frozen=True)
class RelationSpec:
    name: str
    category: RelationCategory
    description: str = ""


RELATIONS: Dict[str, RelationSpec] = {}


def register_relation(
    name: str, category: RelationCategory, description: str = ""
) -> RelationSpec:
    """Register or replace a relation type."""
    if not name or not name.strip():
        raise ValueError("relation name must be non-empty")
    spec = RelationSpec(name, category, description)
    RELATIONS[name] = spec
    return spec


def relation_category(name: str) -> Optional[RelationCategory]:
    spec = RELATIONS.get(name)
    return spec.category if spec else None


def is_known_relation(name: str) -> bool:
    return name in RELATIONS


def relations_in(category: RelationCategory) -> FrozenSet[str]:
    return frozenset(n for n, s in RELATIONS.items() if s.category == category)


for _name in ("follows", "derived_from", "extracted_from", "references"):
    register_relation(_name, RelationCategory.PROVENANCE)
for _name in ("supersedes", "contradicts"):
    register_relation(_name, RelationCategory.CONTRADICTION)
for _name in (
    "depends_on", "based_on", "summary_of", "summarizes", "caused_by",
    "duplicate_of", "related_to",
):
    register_relation(_name, RelationCategory.STRUCTURAL)
for _name in ("cites", "supports", "extends", "evaluated_on"):
    register_relation(_name, RelationCategory.RESEARCH)
del _name


# Memory types from ctxflow.memory.store.MemoryType plus research node types.
NODE_TYPES: FrozenSet[str] = frozenset({
    "conversation", "fact", "decision", "observation", "tool_result",
    "document", "task", "correction", "summary", "user_preference",
    "claim", "paper", "dataset", "analysis", "artifact", "generic",
})


def is_known_node_type(name: str) -> bool:
    return name in NODE_TYPES
