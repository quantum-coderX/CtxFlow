"""Skill Registry — typed, schema-validated tool abstractions.

Provides a central registry of invokable skills with Pydantic-based
input/output validation. Skills are registered with a schema and can
be discovered, validated, and invoked by agents at runtime.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill schema definitions
# ---------------------------------------------------------------------------

class SkillInputSchema(BaseModel):
    """JSON-Schema-compatible input descriptor for a skill."""

    model_config = ConfigDict(extra="allow")


class SkillOutputSchema(BaseModel):
    """JSON-Schema-compatible output descriptor for a skill."""

    model_config = ConfigDict(extra="allow")


class SkillSchema(BaseModel):
    """Full schema descriptor for a registered skill.

    Attributes
    ----------
    name : str
        Unique skill identifier (e.g. ``"literature_search"``).
    description : str
        Human-readable description of what the skill does.
    input_schema : dict
        JSON Schema defining valid inputs.
    output_schema : dict
        JSON Schema defining the output format.
    tags : list[str]
        Searchable tags for skill discovery.
    version : str
        Semantic version of the skill.
    """

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    version: str = "0.1.0"


class SkillResult(BaseModel):
    """Standardized result envelope from a skill invocation.

    Attributes
    ----------
    skill_name : str
        The skill that produced this result.
    success : bool
        Whether the invocation succeeded.
    data : Any
        The actual output payload.
    error : str | None
        Error message if the invocation failed.
    metadata : dict
        Auxiliary information (latency, cache hit, etc.).
    """

    skill_name: str
    success: bool = True
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Callable type for skill functions: sync or async
SkillCallable = Union[
    Callable[..., Any],
    Callable[..., Coroutine[Any, Any, Any]],
]


# ---------------------------------------------------------------------------
# Lightweight JSON-Schema validation
# ---------------------------------------------------------------------------

_TYPE_CHECKS: Dict[str, tuple] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _matches_type(value: Any, expected: Any) -> bool:
    """Return True when *value* matches a JSON-Schema ``type`` declaration."""
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    checks = _TYPE_CHECKS.get(str(expected))
    if checks is None:
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, checks)


def validate_input(
    schema: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """Validate *payload* against a minimal JSON-Schema object.

    Returns an error message, or ``None`` when the payload is valid. This
    intentionally supports the subset used by the skill registry
    (``type``, ``properties``, ``required``) rather than pulling in a full
    JSON-Schema implementation.
    """
    if not schema:
        return None

    expected_type = schema.get("type")
    if expected_type and not _matches_type(payload, expected_type):
        return f"Input must be of type {expected_type}."

    required = schema.get("required", []) or []
    for key in required:
        if key not in payload:
            return f"Missing required input: '{key}'."

    properties = schema.get("properties", {}) or {}
    for key, spec in properties.items():
        if key not in payload or not isinstance(spec, dict):
            continue
        declared = spec.get("type")
        if declared and not _matches_type(payload[key], declared):
            return (
                f"Input '{key}' must be of type {declared}, "
                f"got {type(payload[key]).__name__}."
            )
        minimum = spec.get("minimum")
        if minimum is not None and isinstance(payload[key], (int, float)):
            if payload[key] < minimum:
                return f"Input '{key}' must be >= {minimum}."
        allowed = spec.get("enum")
        if allowed and payload[key] not in allowed:
            return f"Input '{key}' must be one of {allowed}."

    return None


# ---------------------------------------------------------------------------
# Skill Registry
# ---------------------------------------------------------------------------

class _RegisteredSkill:
    """Internal wrapper for a registered skill."""

    __slots__ = ("schema", "fn", "is_async")

    def __init__(
        self, schema: SkillSchema, fn: SkillCallable, is_async: bool
    ) -> None:
        self.schema = schema
        self.fn = fn
        self.is_async = is_async


class SkillRegistry:
    """Central registry for typed, schema-validated skills.

    Usage::

        registry = SkillRegistry()

        @registry.register(
            name="literature_search",
            description="Search arXiv and Semantic Scholar",
            input_schema={"query": {"type": "string"}},
            tags=["search", "arxiv"],
        )
        def literature_search(query: str, max_results: int = 5):
            ...

        result = registry.invoke("literature_search", query="attention")
    """

    def __init__(self) -> None:
        self._skills: Dict[str, _RegisteredSkill] = {}

    # -- Registration -------------------------------------------------------

    def register(
        self,
        name: str,
        description: str = "",
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        version: str = "0.1.0",
    ) -> Callable:
        """Decorator to register a skill function.

        Parameters
        ----------
        name : str
            Unique skill name.
        description : str
            Human-readable description.
        input_schema : dict | None
            JSON Schema for inputs.
        output_schema : dict | None
            JSON Schema for outputs.
        tags : list[str] | None
            Searchable tags.
        version : str
            Skill version.
        """

        def decorator(fn: SkillCallable) -> SkillCallable:
            import asyncio
            is_async = asyncio.iscoroutinefunction(fn)

            schema = SkillSchema(
                name=name,
                description=description,
                input_schema=input_schema or {},
                output_schema=output_schema or {},
                tags=tags or [],
                version=version,
            )

            self._skills[name] = _RegisteredSkill(
                schema=schema, fn=fn, is_async=is_async
            )
            logger.info("Registered skill: %s (v%s)", name, version)
            return fn

        return decorator

    def register_function(
        self,
        name: str,
        fn: SkillCallable,
        description: str = "",
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        version: str = "0.1.0",
    ) -> None:
        """Programmatically register a skill function (non-decorator)."""
        import asyncio

        schema = SkillSchema(
            name=name,
            description=description,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            tags=tags or [],
            version=version,
        )
        self._skills[name] = _RegisteredSkill(
            schema=schema,
            fn=fn,
            is_async=asyncio.iscoroutinefunction(fn),
        )

    # -- Discovery ----------------------------------------------------------

    def list_skills(self) -> List[SkillSchema]:
        """Return schemas of all registered skills."""
        return [s.schema for s in self._skills.values()]

    def get_schema(self, name: str) -> Optional[SkillSchema]:
        """Return the schema for *name*, or ``None``."""
        entry = self._skills.get(name)
        return entry.schema if entry else None

    def has_skill(self, name: str) -> bool:
        return name in self._skills

    def find_by_tag(self, tag: str) -> List[SkillSchema]:
        """Find skills that have *tag* in their tags list."""
        return [
            s.schema for s in self._skills.values()
            if tag in s.schema.tags
        ]

    # -- Invocation ---------------------------------------------------------

    def invoke(self, name: str, /, **kwargs: Any) -> SkillResult:
        """Synchronously invoke a registered skill.

        Parameters
        ----------
        name : str
            The skill to invoke.
        **kwargs
            Arguments passed to the skill function.

        Returns
        -------
        SkillResult
            Standardized result envelope.
        """
        entry = self._skills.get(name)
        if entry is None:
            return SkillResult(
                skill_name=name,
                success=False,
                error=f"Skill '{name}' not found in registry.",
            )

        if entry.is_async:
            return SkillResult(
                skill_name=name,
                success=False,
                error=f"Skill '{name}' is async — use invoke_async() instead.",
            )

        validation_error = validate_input(entry.schema.input_schema, kwargs)
        if validation_error:
            return SkillResult(
                skill_name=name, success=False, error=validation_error
            )

        try:
            data = entry.fn(**kwargs)
            return SkillResult(skill_name=name, success=True, data=data)
        except Exception as exc:
            logger.error("Skill '%s' failed: %s", name, exc, exc_info=True)
            return SkillResult(
                skill_name=name, success=False, error=str(exc)
            )

    async def invoke_async(self, name: str, /, **kwargs: Any) -> SkillResult:
        """Asynchronously invoke a registered skill.

        Works for both sync and async skill functions.
        """
        import asyncio

        entry = self._skills.get(name)
        if entry is None:
            return SkillResult(
                skill_name=name,
                success=False,
                error=f"Skill '{name}' not found in registry.",
            )

        validation_error = validate_input(entry.schema.input_schema, kwargs)
        if validation_error:
            return SkillResult(
                skill_name=name, success=False, error=validation_error
            )

        try:
            if entry.is_async:
                data = await entry.fn(**kwargs)
            else:
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, lambda: entry.fn(**kwargs))
            return SkillResult(skill_name=name, success=True, data=data)
        except Exception as exc:
            logger.error("Skill '%s' failed: %s", name, exc, exc_info=True)
            return SkillResult(
                skill_name=name, success=False, error=str(exc)
            )

    # -- Utility ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __repr__(self) -> str:
        names = ", ".join(sorted(self._skills.keys()))
        return f"SkillRegistry(skills=[{names}])"
