"""Bounded execution of validated, dependency-aware ResearchOS tool plans."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from researchos.guardrails import allow_tool, validate_tool_args
from researchos.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_TASKS = 32


class HarnessPlanError(ValueError):
    """A proposed tool plan is malformed or has invalid dependencies."""


@dataclass(frozen=True)
class ToolTask:
    """One named tool invocation in a dependency-aware plan.

    An argument may contain ``{"$ref": "task_id.path.to.value"}`` to consume
    another task's output. References are resolved as data, never evaluated.
    """

    task_id: str
    tool_name: str
    args: Dict[str, Any] = field(default_factory=dict)
    depends_on: Tuple[str, ...] = ()


@dataclass
class ToolTaskResult:
    """Auditable status and output for one planned tool invocation."""

    task_id: str
    tool_name: str
    status: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class HarnessResult:
    """Result envelope for a complete tool plan."""

    success: bool
    status: str
    tasks: List[ToolTaskResult]
    duration_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "tasks": [task.to_dict() for task in self.tasks],
            "duration_ms": self.duration_ms,
        }


class ToolHarness:
    """Execute fixed-schema registered skills with bounded parallelism.

    The harness does not execute code supplied in a plan. The only executable
    operations are functions already registered in :class:`SkillRegistry` and
    permitted by ResearchOS's tool allowlist.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        max_parallel: int = 4,
        max_tasks: int = _MAX_TASKS,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1.")
        if max_tasks < 1 or max_tasks > _MAX_TASKS:
            raise ValueError(f"max_tasks must be between 1 and {_MAX_TASKS}.")
        self.registry = registry
        self.max_parallel = max_parallel
        self.max_tasks = max_tasks

    async def run(self, tasks: Sequence[ToolTask]) -> HarnessResult:
        """Validate then execute a DAG of tool calls.

        A failed task blocks its dependents, while unrelated tasks still run.
        The final task list preserves the plan's input order.
        """
        plan = tuple(tasks)
        self._validate_plan(plan)
        started = time.monotonic()
        results: Dict[str, ToolTaskResult] = {}
        pending = {task.task_id for task in plan}
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def execute(task: ToolTask) -> ToolTaskResult:
            denied = allow_tool(task.tool_name)
            if not denied.allowed:
                return ToolTaskResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    status="failed",
                    success=False,
                    error=denied.reason,
                )
            if not self.registry.has_skill(task.tool_name):
                return ToolTaskResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    status="failed",
                    success=False,
                    error=f"Tool {task.tool_name!r} is not registered.",
                )

            try:
                resolved_args = self._resolve_args(task.args, task, results)
            except HarnessPlanError as exc:
                return ToolTaskResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    status="failed",
                    success=False,
                    error=str(exc),
                )

            guard = validate_tool_args(task.tool_name, resolved_args)
            if not guard.allowed:
                return ToolTaskResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    status="failed",
                    success=False,
                    error=guard.reason,
                )

            call_started = time.monotonic()
            async with semaphore:
                skill_result = await self.registry.invoke_async(
                    task.tool_name, **resolved_args
                )
            duration_ms = (time.monotonic() - call_started) * 1000
            return ToolTaskResult(
                task_id=task.task_id,
                tool_name=task.tool_name,
                status="completed" if skill_result.success else "failed",
                success=skill_result.success,
                data=skill_result.data,
                error=skill_result.error,
                duration_ms=duration_ms,
            )

        while pending:
            ready = [
                task for task in plan
                if task.task_id in pending
                and all(dep in results for dep in task.depends_on)
            ]
            if not ready:
                raise HarnessPlanError("Tool plan has unresolved dependencies.")

            runnable: List[ToolTask] = []
            for task in ready:
                failed_dependencies = [
                    dep for dep in task.depends_on if not results[dep].success
                ]
                if failed_dependencies:
                    results[task.task_id] = ToolTaskResult(
                        task_id=task.task_id,
                        tool_name=task.tool_name,
                        status="blocked",
                        success=False,
                        error=(
                            "Blocked by unsuccessful dependencies: "
                            + ", ".join(failed_dependencies)
                        ),
                    )
                    pending.remove(task.task_id)
                else:
                    runnable.append(task)

            if runnable:
                completed = await asyncio.gather(
                    *(execute(task) for task in runnable)
                )
                for task_result in completed:
                    results[task_result.task_id] = task_result
                    pending.remove(task_result.task_id)

        ordered_results = [results[task.task_id] for task in plan]
        success = all(result.success for result in ordered_results)
        any_completed = any(result.status == "completed" for result in ordered_results)
        status = "completed" if success else ("partial" if any_completed else "failed")
        return HarnessResult(
            success=success,
            status=status,
            tasks=ordered_results,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _validate_plan(self, tasks: Tuple[ToolTask, ...]) -> Dict[str, ToolTask]:
        if not tasks:
            raise HarnessPlanError("Tool plan must contain at least one task.")
        if len(tasks) > self.max_tasks:
            raise HarnessPlanError(
                f"Tool plan exceeds the {self.max_tasks}-task limit."
            )

        task_map: Dict[str, ToolTask] = {}
        for task in tasks:
            if not isinstance(task, ToolTask):
                raise HarnessPlanError("Every plan entry must be a ToolTask.")
            if not isinstance(task.task_id, str) or not _TASK_ID.fullmatch(task.task_id):
                raise HarnessPlanError(f"Invalid task id: {task.task_id!r}.")
            if task.task_id in task_map:
                raise HarnessPlanError(f"Duplicate task id: {task.task_id!r}.")
            if not isinstance(task.tool_name, str) or not task.tool_name.strip():
                raise HarnessPlanError(f"Task {task.task_id!r} has no tool name.")
            if not isinstance(task.args, dict):
                raise HarnessPlanError(f"Task {task.task_id!r} args must be an object.")
            if not isinstance(task.depends_on, (tuple, list)) or not all(
                isinstance(dependency, str) for dependency in task.depends_on
            ):
                raise HarnessPlanError(
                    f"Task {task.task_id!r} dependencies must be task ids."
                )
            if len(set(task.depends_on)) != len(task.depends_on):
                raise HarnessPlanError(
                    f"Task {task.task_id!r} has duplicate dependencies."
                )
            task_map[task.task_id] = task

        for task in tasks:
            for dependency in task.depends_on:
                if dependency not in task_map:
                    raise HarnessPlanError(
                        f"Task {task.task_id!r} depends on unknown task {dependency!r}."
                    )
                if dependency == task.task_id:
                    raise HarnessPlanError(
                        f"Task {task.task_id!r} cannot depend on itself."
                    )
            for reference in self._references(task.args):
                referenced_task = reference.split(".", 1)[0]
                if referenced_task not in task.depends_on:
                    raise HarnessPlanError(
                        f"Task {task.task_id!r} references {referenced_task!r} "
                        "without declaring it as a dependency."
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise HarnessPlanError("Tool plan contains a dependency cycle.")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in task_map[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_map:
            visit(task_id)
        return task_map

    @classmethod
    def _references(cls, value: Any) -> List[str]:
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                reference = value["$ref"]
                if not isinstance(reference, str) or not reference.strip():
                    raise HarnessPlanError("Task output references must be non-empty strings.")
                if any(not part for part in reference.split(".")):
                    raise HarnessPlanError(f"Invalid task output reference: {reference!r}.")
                return [reference]
            return [
                reference
                for nested in value.values()
                for reference in cls._references(nested)
            ]
        if isinstance(value, (list, tuple)):
            return [
                reference
                for nested in value
                for reference in cls._references(nested)
            ]
        return []

    @classmethod
    def _resolve_args(
        cls,
        value: Any,
        task: ToolTask,
        results: Mapping[str, ToolTaskResult],
    ) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                reference = value["$ref"]
                parts = reference.split(".")
                task_id, path = parts[0], parts[1:]
                if task_id not in results:
                    raise HarnessPlanError(
                        f"Task {task.task_id!r} output {task_id!r} is not available."
                    )
                resolved = results[task_id].data
                for segment in path:
                    if isinstance(resolved, Mapping) and segment in resolved:
                        resolved = resolved[segment]
                    elif isinstance(resolved, (list, tuple)) and segment.isdigit():
                        index = int(segment)
                        if index >= len(resolved):
                            raise HarnessPlanError(
                                f"Reference {reference!r} has an out-of-range index."
                            )
                        resolved = resolved[index]
                    else:
                        raise HarnessPlanError(
                            f"Reference {reference!r} could not resolve segment {segment!r}."
                        )
                return resolved
            return {
                key: cls._resolve_args(nested, task, results)
                for key, nested in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                cls._resolve_args(nested, task, results) for nested in value
            ]
        return value
