"""Test plan for ResearchOS run lifecycle (state machine, resume, failures).

Sprint S0 — stubs and design notes only. Tests are marked ``xfail`` until
the corresponding feature lands in S1/S2/S3.

Run with:
    python -m pytest -q tests/test_runs.py

Each test class maps to a P0 deliverable from HARIKESH.md §5.
"""

from __future__ import annotations

import pytest

from researchos.runs import (
    InvalidRunTransition,
    PlanStep,
    RunEvent,
    RunRecord,
    RunState,
    hash_payload,
    validate_transition,
)


# ===========================================================================
# RunState transitions (P0 deliverable #2)
# ===========================================================================

class TestRunStateTransitions:
    """State machine: valid transitions accepted, invalid ones rejected."""

    def test_planning_to_running(self):
        """Happy path: no confirmation gate."""
        rec = RunRecord(query="test query")
        assert rec.state == RunState.PLANNING
        rec.transition_to(RunState.RUNNING)
        assert rec.state == RunState.RUNNING

    def test_planning_to_awaiting_then_running(self):
        """With confirmation gate."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.AWAITING_CONFIRMATION)
        rec.transition_to(RunState.RUNNING)
        assert rec.state == RunState.RUNNING

    def test_running_to_completed(self):
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.COMPLETED)
        assert rec.state == RunState.COMPLETED

    def test_running_to_failed(self):
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.FAILED)
        assert rec.state == RunState.FAILED

    def test_failed_to_running_resume(self):
        """Resume: FAILED -> RUNNING is valid."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.FAILED)
        rec.transition_to(RunState.RUNNING)
        assert rec.state == RunState.RUNNING

    def test_invalid_completed_to_running(self):
        """COMPLETED is terminal; no further transitions allowed."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.COMPLETED)
        with pytest.raises(InvalidRunTransition):
            rec.transition_to(RunState.RUNNING)

    def test_invalid_running_to_planning(self):
        """Cannot go backward to PLANNING."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        with pytest.raises(InvalidRunTransition):
            rec.transition_to(RunState.PLANNING)

    def test_invalid_cancelled_to_any(self):
        """CANCELLED is terminal."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.CANCELLED)
        for state in RunState:
            if state == RunState.CANCELLED:
                continue
            with pytest.raises(InvalidRunTransition):
                rec.transition_to(state)

    def test_transition_emits_event(self):
        """Each transition must emit a state_transition event."""
        rec = RunRecord(query="test query")
        evt = rec.transition_to(RunState.RUNNING)
        assert isinstance(evt, RunEvent)
        assert evt.type == "state_transition"
        assert evt.run_id == rec.run_id
        assert evt.seq == 1

    def test_events_are_sequential(self):
        """Sequence numbers must be strictly increasing."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.AWAITING_CONFIRMATION)
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.COMPLETED)
        seqs = [e.seq for e in rec.events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

    def test_is_terminal_completed(self):
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.COMPLETED)
        assert rec.is_terminal is True

    def test_is_terminal_cancelled(self):
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.transition_to(RunState.CANCELLED)
        assert rec.is_terminal is True

    def test_is_not_terminal_running(self):
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        assert rec.is_terminal is False


# ===========================================================================
# Plan freezing (P0 deliverable #3)
# ===========================================================================

class TestPlanImmutability:
    """Plan never mutates mid-run."""

    def test_planstep_is_frozen(self):
        """PlanStep is a frozen dataclass — setting any field raises."""
        step = PlanStep(agent="synthesizer", purpose="Synthesize answer")
        with pytest.raises((AttributeError, TypeError)):
            step.agent = "critic"  # type: ignore[misc]

    def test_plan_list_set_once(self):
        """Plan is recorded and accessible immediately."""
        steps = [
            PlanStep(agent="retriever", purpose="Retrieve papers"),
            PlanStep(agent="synthesizer", purpose="Synthesize"),
        ]
        rec = RunRecord(query="q", plan=steps)
        assert rec.plan == steps
        assert len(rec.plan) == 2

    def test_run_event_is_frozen(self):
        """RunEvent must be immutable once created."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        evt = rec.events[0]
        with pytest.raises((AttributeError, TypeError)):
            evt.type = "mutated"  # type: ignore[misc]


# ===========================================================================
# Resume support (P0 deliverable #5)
# ===========================================================================

class TestResumeSupport:
    """Killed/restarted runs resume from persisted step state."""

    def test_record_and_check_step(self):
        """Step results are recorded and detectable."""
        rec = RunRecord(query="test query")
        rec.record_step_result("retriever", {"internal_results": ["node1"]})
        assert rec.is_step_complete("retriever")
        assert not rec.is_step_complete("synthesizer")

    def test_completed_agents_ordered(self):
        """completed_agents() preserves insertion order."""
        rec = RunRecord(query="test query")
        rec.record_step_result("retriever", {})
        rec.record_step_result("reader", {})
        assert rec.completed_agents() == ["retriever", "reader"]

    @pytest.mark.xfail(
        reason="Resume logic not yet wired into SkillRouter (S3 deliverable)",
        strict=False,
    )
    def test_resume_skips_completed_steps(self):
        """A resumed run should not re-run completed retrieval/reading steps.

        S3 implementation: when a RunRecord is loaded with step_results
        populated, SkillRouter must skip those agents and continue from
        the first incomplete step.
        """
        raise NotImplementedError("S3: implement resume logic in SkillRouter")

    @pytest.mark.xfail(
        reason="Process-restart persistence not yet implemented (S3)",
        strict=False,
    )
    def test_resume_after_process_restart(self):
        """After a process kill and restart, the run resumes correctly.

        S3 implementation: serialise RunRecord to Christo's run_events table,
        deserialise on startup, and continue from the last completed step.
        """
        raise NotImplementedError("S3: implement persist/load via Christo's store")


# ===========================================================================
# Failure visibility (P0 deliverable #6)
# ===========================================================================

class TestFailureVisibility:
    """Agent failures must surface as FAILED state, never silent success."""

    def test_emit_error_event(self):
        """An error event is emitted with the agent name and message."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        evt = rec.emit_error(agent="retriever", error="timeout connecting to provider")
        assert evt.type == "error"
        assert evt.agent == "retriever"
        assert "timeout" in (evt.error or "")

    def test_failed_state_after_agent_error(self):
        """After an agent error, the run must move to FAILED (not COMPLETED)."""
        rec = RunRecord(query="test query")
        rec.transition_to(RunState.RUNNING)
        rec.emit_error(agent="synthesizer", error="LLM unavailable")
        rec.transition_to(RunState.FAILED)
        assert rec.state == RunState.FAILED

    @pytest.mark.xfail(
        reason="/api/chat partial-failure propagation not yet implemented (S1)",
        strict=False,
    )
    def test_api_chat_returns_failed_status_on_agent_error(self):
        """POST /api/chat must return a non-200 or labelled partial result
        when one or more agents fail. Currently the router swallows failures
        silently (HARIKESH.md §12 landmine).

        S1 implementation: update SkillRouter to surface partial results with
        success=False, and update /api/chat to propagate this correctly.
        """
        raise NotImplementedError("S1: surface agent failures in /api/chat")


# ===========================================================================
# Bounded retry policy (P0 deliverable #8)
# ===========================================================================

class TestRetryPolicy:
    """max_attempts=2, exponential 0.5s, transient/timeout/rate-limit only."""

    def test_planstep_default_max_attempts(self):
        step = PlanStep(agent="retriever", purpose="Retrieve")
        assert step.max_attempts == 2

    def test_planstep_custom_max_attempts(self):
        """Custom attempt limits are accepted at plan creation time only."""
        step = PlanStep(agent="synthesizer", purpose="Synth", max_attempts=1)
        assert step.max_attempts == 1

    @pytest.mark.xfail(
        reason="Retry executor not yet implemented in SkillRouter (S2)",
        strict=False,
    )
    def test_retry_bounded_at_max_attempts(self):
        """A transient error must be retried at most max_attempts times.

        S2 implementation: add a retry executor to SkillRouter that calls
        agent.run() up to PlanStep.max_attempts times with exponential
        back-off (initial=0.5s), then marks the step FAILED.
        """
        raise NotImplementedError("S2: implement bounded retry in SkillRouter")

    @pytest.mark.xfail(
        reason="Retry classification not yet implemented (S2)",
        strict=False,
    )
    def test_non_transient_error_not_retried(self):
        """Permanent errors (e.g., invalid query) must NOT trigger a retry.

        S2 implementation: classify exceptions into transient (timeout,
        rate-limit, network) vs permanent (validation, missing data) and
        only retry transient ones.
        """
        raise NotImplementedError("S2: classify transient vs permanent errors")


# ===========================================================================
# Event loop safety (P0 deliverable #7)
# ===========================================================================

class TestEventLoopSafety:
    """Synchronous run() must not block the async event loop."""

    @pytest.mark.xfail(
        reason="Blocking I/O fix not yet implemented in /api/chat (S1)",
        strict=False,
    )
    def test_no_blocking_io_in_chat_endpoint(self):
        """POST /api/chat must not call SkillRouter.run() directly in an
        async def — it must use asyncio.to_thread() or loop.run_in_executor().

        S1 implementation: fix src/researchos/api/routes/chat.py:54.
        """
        raise NotImplementedError("S1: fix blocking run() in async chat handler")


# ===========================================================================
# Hash payload utility
# ===========================================================================

class TestHashPayload:
    """hash_payload() produces stable, non-empty digests."""

    def test_deterministic(self):
        assert hash_payload("hello") == hash_payload("hello")

    def test_different_inputs_different_hashes(self):
        assert hash_payload("hello") != hash_payload("world")

    def test_returns_hex_string(self):
        h = hash_payload("test prompt")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex
        int(h, 16)  # raises ValueError if not hex


# ===========================================================================
# Validate transition helper
# ===========================================================================

class TestValidateTransition:
    """validate_transition() is the shared guard used by all state changes."""

    def test_valid_transition_does_not_raise(self):
        validate_transition(RunState.PLANNING, RunState.RUNNING)

    def test_invalid_transition_raises(self):
        with pytest.raises(InvalidRunTransition) as exc_info:
            validate_transition(RunState.COMPLETED, RunState.RUNNING)
        assert exc_info.value.current == RunState.COMPLETED
        assert exc_info.value.requested == RunState.RUNNING
