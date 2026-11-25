# ADR-001: Orchestration runtime

**Status:** ACCEPTED (2026-11-23)

## Decision

- LangGraph is the default research workflow engine (`ResearchGraph`).
- The sequential engine (`ResearchWorkflow`) remains an explicit fallback, selected with
  `RESEARCHOS_ORCHESTRATOR=sequential` or used when LangGraph is not installed.
- Both engines share the eight-stage contract
  `plan -> retrieve -> read -> memory -> pack -> synthesize -> verify -> persist` and return
  the same result shape.
- The LangGraph checkpointer is `MemorySaver`/`InMemorySaver`, keyed by `run_id`. It is
  process-local inspection only and is not durable resume.

## Consequences

- No restart or resume capability is promised. Adding one needs a persistent checkpointer,
  its schema and lifecycle, and an end-to-end restart test.
- Run outcomes and traces are persisted separately through `ResearchWorkspace`.
- Engine parity is enforced by tests in `tests/test_langgraph_workflow.py` and
  `tests/test_memory_failure_modes.py`.

## Supersedes

The earlier proposal that forbade adding LangGraph before approval.
