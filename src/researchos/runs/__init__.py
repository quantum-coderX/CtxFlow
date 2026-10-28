"""Run lifecycle contracts for ResearchOS.

Defines the shared types published by the orchestration owner (Harikesh)
for Sprint S0. These are the *stable* contracts that all workstreams consume:

- ``RunState`` — the six explicit states of a research run.
- ``PlanStep`` — one frozen step in an execution plan.
- ``RunEvent`` — an immutable audit event emitted at each lifecycle point.
- ``RunRecord`` — in-memory run envelope (not the persistence schema; see
  Christo's ``run_events`` tables for storage).

Design rules (HARIKESH.md §7, §8):
- ``plan`` is frozen after creation; steps are never mutated mid-run.
- Invalid state transitions raise ``InvalidRunTransition``.
- No model call on the transition path; all logic is deterministic.
- ``RunEvent`` never stores raw prompts or secrets; only hashes are recorded.
- ``CtxFlow.export()`` is graph-only and cannot serve as a run store.
  ``RunRecord`` is separate from CtxFlow serialization.

These types are intentionally dependency-light (stdlib only) so that
any workstream can import them without pulling in FastAPI, CtxFlow, or
agent machinery.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Run state machine
# ---------------------------------------------------------------------------

class RunState(str, Enum):
    """The six explicit states of a research run.

    Valid transitions::

        PLANNING -> AWAITING_CONFIRMATION
        PLANNING -> RUNNING               (if confirmation is skipped)
        AWAITING_CONFIRMATION -> RUNNING
        AWAITING_CONFIRMATION -> CANCELLED
        RUNNING -> COMPLETED
        RUNNING -> FAILED
        RUNNING -> CANCELLED
        FAILED  -> RUNNING               (resume: re-enter from last checkpoint)
        COMPLETED -> (terminal)
        CANCELLED -> (terminal)
    """

    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Map: current state -> set of states reachable from it.
_VALID_TRANSITIONS: Dict[RunState, frozenset] = {
    RunState.PLANNING: frozenset({
        RunState.AWAITING_CONFIRMATION,
        RunState.RUNNING,
        RunState.CANCELLED,
    }),
    RunState.AWAITING_CONFIRMATION: frozenset({
        RunState.RUNNING,
        RunState.CANCELLED,
    }),
    RunState.RUNNING: frozenset({
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
    }),
    RunState.FAILED: frozenset({
        RunState.RUNNING,   # resume without re-running expensive steps
    }),
    RunState.COMPLETED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


class InvalidRunTransition(ValueError):
    """Raised when a run is asked to move to an unreachable state."""

    def __init__(self, current: RunState, requested: RunState) -> None:
        super().__init__(
            f"Cannot transition from {current.value!r} to {requested.value!r}. "
            f"Valid from {current.value!r}: "
            f"{sorted(s.value for s in _VALID_TRANSITIONS[current])!r}"
        )
        self.current = current
        self.requested = requested


def validate_transition(current: RunState, requested: RunState) -> None:
    """Raise :class:`InvalidRunTransition` if the transition is not allowed.

    Parameters
    ----------
    current:
        The run's present state.
    requested:
        The state being requested.

    Raises
    ------
    InvalidRunTransition
        If the transition is not in the allowed set.
    """
    if requested not in _VALID_TRANSITIONS.get(current, frozenset()):
        raise InvalidRunTransition(current, requested)


# ---------------------------------------------------------------------------
# Plan step
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanStep:
    """A single frozen step in an execution plan.

    Plans are immutable after creation (frozen=True). A repair loop is
    implemented as a counter on the same step, not as a new plan entry.

    Attributes
    ----------
    agent:
        Name of the agent to invoke (must be registered in the router).
    purpose:
        Short human-readable description of what this step does.
    allowed_tools:
        Tuple of tool names the agent is permitted to call (least-privilege).
        Empty tuple means the agent makes no tool calls (e.g., Synthesizer
        when using only in-memory context).
    max_attempts:
        Maximum number of execution attempts before the step is marked failed.
        Set to 2 per the bounded retry policy (HARIKESH.md §5 deliverable #8).
    """

    agent: str
    purpose: str
    allowed_tools: Tuple[str, ...] = ()
    max_attempts: int = 2


# ---------------------------------------------------------------------------
# Run event
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunEvent:
    """An immutable audit event emitted at each lifecycle point.

    Consumers (Christo's persistence layer, Abinav's eval harness) receive
    a stream of these events; they must never mutate them.

    Privacy rules (HARIKESH.md §8 point 9):
    - ``prompt_hash``: SHA-256 of the raw prompt (never the prompt itself).
    - ``args_hash``: SHA-256 of the serialised tool arguments.
    - ``output_hash``: SHA-256 of the raw output string.

    Attributes
    ----------
    run_id:
        UUID of the owning run.
    seq:
        Monotonically increasing sequence number within the run.
    ts:
        Wall-clock timestamp (seconds since epoch).
    type:
        Event type. One of:
        ``route`` | ``agent_start`` | ``agent_end`` | ``tool_call``
        | ``guard`` | ``state_transition`` | ``error``.
    agent:
        Agent name (None for route/guard events).
    prompt_hash:
        SHA-256 hex of the prompt sent to the model (None if no model call).
    model_id:
        Model identifier string (None if no model call).
    tool:
        Tool name (None if not a tool_call event).
    args_hash:
        SHA-256 hex of the serialised tool args (None if not a tool_call).
    output_hash:
        SHA-256 hex of the raw output (None if the agent produced no output).
    duration_ms:
        Wall-clock duration in milliseconds (None for start events).
    cache_hit:
        Whether the result was served from cache (None if not applicable).
    attempt:
        Retry attempt number (1-indexed; None for non-agent events).
    error:
        Error message for ``error`` events; None otherwise.
    """

    run_id: str
    seq: int
    ts: float
    type: str  # route|agent_start|agent_end|tool_call|guard|state_transition|error
    agent: Optional[str] = None
    prompt_hash: Optional[str] = None
    model_id: Optional[str] = None
    tool: Optional[str] = None
    args_hash: Optional[str] = None
    output_hash: Optional[str] = None
    duration_ms: Optional[float] = None
    cache_hit: Optional[bool] = None
    attempt: Optional[int] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict for JSON emission / persistence."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of *text* (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_payload(payload: str) -> str:
    """Hash a prompt, args, or output string for safe storage.

    Use this instead of storing raw payloads in ``RunEvent``.
    """
    return _sha256(payload)


# ---------------------------------------------------------------------------
# Run record (in-memory envelope)
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    """In-memory envelope for a single research run.

    This is the run store consumed by orchestration code. It is
    intentionally separate from the CtxFlow graph (``CtxFlow.export()``
    is graph-only and cannot serve as a run store — HARIKESH.md §8 pt 11).

    Persistence: pass this to Christo's ``RunEventStore.save()`` — do not
    invent a second DB schema.

    Attributes
    ----------
    run_id:
        UUID generated at run creation.
    query:
        The original user query (stored once at creation).
    intent:
        Classified ``QueryIntent`` value (set during PLANNING).
    plan:
        Ordered, frozen list of ``PlanStep``s.  Set once; never mutated.
    state:
        Current ``RunState``. Use :meth:`transition_to` to update.
    events:
        Ordered list of ``RunEvent``s emitted during this run.
    step_results:
        Mapping of ``agent_name -> result_data`` for completed steps.
        Used by resume logic to skip already-completed expensive steps.
    created_at:
        Wall-clock timestamp when the run record was created.
    updated_at:
        Wall-clock timestamp of the last state transition.
    failure_reason:
        Error message if the run moved to FAILED; None otherwise.
    session_id:
        Optional caller-supplied session identifier for grouping.
    """

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    query: str = ""
    intent: str = ""
    plan: List[PlanStep] = field(default_factory=list)
    state: RunState = RunState.PLANNING
    events: List[RunEvent] = field(default_factory=list)
    step_results: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    failure_reason: Optional[str] = None
    session_id: Optional[str] = None

    # Internal sequence counter for events.
    _seq: int = field(default=0, repr=False)

    # ---------------------------------------------------------------------------
    # State machine
    # ---------------------------------------------------------------------------

    def transition_to(self, new_state: RunState) -> RunEvent:
        """Transition the run to *new_state*, emitting a state_transition event.

        Parameters
        ----------
        new_state:
            The target state.

        Returns
        -------
        RunEvent
            The emitted ``state_transition`` event (also appended to ``self.events``).

        Raises
        ------
        InvalidRunTransition
            If the transition is not valid.
        """
        validate_transition(self.state, new_state)
        old_state = self.state
        self.state = new_state
        self.updated_at = time.time()
        evt = self._emit_event(
            type="state_transition",
            error=f"{old_state.value} -> {new_state.value}",
        )
        return evt

    # ---------------------------------------------------------------------------
    # Event emission helpers
    # ---------------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_event(self, **kwargs: Any) -> RunEvent:
        """Create, record, and return a new RunEvent."""
        evt = RunEvent(
            run_id=self.run_id,
            seq=self._next_seq(),
            ts=time.time(),
            **kwargs,
        )
        self.events.append(evt)
        return evt

    def emit_agent_start(
        self,
        agent: str,
        *,
        attempt: int = 1,
        prompt_hash: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> RunEvent:
        """Emit an ``agent_start`` event."""
        return self._emit_event(
            type="agent_start",
            agent=agent,
            attempt=attempt,
            prompt_hash=prompt_hash,
            model_id=model_id,
        )

    def emit_agent_end(
        self,
        agent: str,
        *,
        duration_ms: float,
        attempt: int = 1,
        output_hash: Optional[str] = None,
        cache_hit: Optional[bool] = None,
    ) -> RunEvent:
        """Emit an ``agent_end`` event."""
        return self._emit_event(
            type="agent_end",
            agent=agent,
            attempt=attempt,
            duration_ms=duration_ms,
            output_hash=output_hash,
            cache_hit=cache_hit,
        )

    def emit_error(self, agent: Optional[str], error: str) -> RunEvent:
        """Emit an ``error`` event (does not change run state)."""
        return self._emit_event(
            type="error",
            agent=agent,
            error=error,
        )

    def emit_guard(self, reason: str) -> RunEvent:
        """Emit a ``guard`` event (input/output guardrail triggered)."""
        return self._emit_event(
            type="guard",
            error=reason,
        )

    # ---------------------------------------------------------------------------
    # Resume helpers
    # ---------------------------------------------------------------------------

    def record_step_result(self, agent: str, data: Any) -> None:
        """Store a completed step's result for resume support.

        Parameters
        ----------
        agent:
            The agent that produced this result.
        data:
            The ``AgentResult.data`` payload. Stored by agent name so that
            a resumed run can skip already-completed expensive steps
            (retrieval, reading) and replay from the first incomplete step.

        Note: Do not store raw model output here — hash it if needed.
        """
        self.step_results[agent] = data

    def is_step_complete(self, agent: str) -> bool:
        """Return True if *agent*'s step already has a recorded result."""
        return agent in self.step_results

    def completed_agents(self) -> List[str]:
        """Return the names of agents with recorded results (in insertion order)."""
        return list(self.step_results.keys())

    # ---------------------------------------------------------------------------
    # Convenience
    # ---------------------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        """True if the run is in a terminal state (COMPLETED or CANCELLED)."""
        return self.state in (RunState.COMPLETED, RunState.CANCELLED)

    def __repr__(self) -> str:
        return (
            f"RunRecord(run_id={self.run_id!r}, "
            f"state={self.state.value!r}, "
            f"query={self.query[:40]!r}, "
            f"steps_done={len(self.step_results)}/{len(self.plan)})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "RunState",
    "InvalidRunTransition",
    "validate_transition",
    "PlanStep",
    "RunEvent",
    "RunRecord",
    "hash_payload",
]
