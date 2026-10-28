# ADR-001 — Orchestration Runtime

| | |
|---|---|
| **Status** | **PROPOSED — awaiting team approval by 30 Oct 2026 18:00** |
| **Deciders** | Harikesh (owner), Adithyan (API/serving), Abinav (eval/UI), Christo (persistence) |
| **Date** | 25 Oct 2026 |
| **Review deadline** | 30 Oct 2026 18:00 (S0 gate) |
| **Constraint** | No LangGraph code enters `pyproject.toml` before this ADR is approved |

---

## 1. Context and Problem Statement

`docs/MVP_IMPLEMENTATION_PLAN.md` §2 promotes LangGraph + `AsyncPostgresSaver` into the MVP as the primary orchestration runtime.

`researchos_team_skills/researchos-orchestration/SKILL.md` (verified at `34cf19f`) states:

> "This repository does **not** use LangGraph, checkpointing, or a Laya model."

Both cannot be simultaneously true. This ADR records the team decision before any code that depends on the choice is written.

### What exists today (verified 25 Oct 2026)

```text
POST /api/chat
  -> SkillRouter.run(query=..., context=...)
      -> classify_intent()          # regex keyword scoring, no LLM call
      -> _EXECUTION_PLANS[intent]   # ordered agent names
      -> run each agent sequentially, thread data forward
  -> return {answer, intent, cited_nodes, alerts, trace?}
```

`AgentStatus` (`idle / running / completed / failed`) exists **per agent**, not per run.
There are no run IDs, run records, state persistence, checkpoints, resume, plan confirmation, retry policy, or cancellation. These are the actual gaps, independent of which option is chosen.

### Known landmines relevant to this decision

| Landmine | Impact on Option A | Impact on Option B |
|---|---|---|
| `/api/chat` calls sync `run()` in `async def` | Fix required either way | Fix required either way |
| No run IDs or state persistence | Small `ResearchRun` record suffices | LangGraph state + `AsyncPostgresSaver` |
| `CtxFlow.export()` is graph-only | Cannot be used as run store either way | Cannot be used as run store either way |
| `previous_results` vs `context["previous_output"]` shape mismatch | Must document precedence rule | Must document precedence rule |
| SSE emits only coarse events | Add per-step events in a targeted PR | Graph nodes emit events natively |
| Module-level `_app_state` in routes | Do not build run state on it | Do not build run state on it |
| Eval harness injects supersession edges manually | Limitation to report | Limitation to report |

---

## 2. Decision Drivers

1. **Demonstrable by M2 (13 Nov 2026)**: Can we show end-to-end pipeline with run states, visible failures, and a resume test within 3 sprints?
2. **Determinism**: Same query + same tool responses -> same plan and same cited node IDs. Required by P0 deliverable #14.
3. **Auditability**: Every state transition must be explicit, validated, and tested. Partial failures must not look like successes.
4. **Cost**: Engineer-days are the scarce resource in a final-year project.
5. **Risk**: A failed demo cannot be recovered in the freeze week.

---

## 3. Options

### Option A — Keep the deterministic `SkillRouter` (default / recommended)

Extend the existing sequential pipeline with explicit run lifecycle without introducing new frameworks.

**What changes:**

- New `src/researchos/runs/` module with `RunState`, `RunRecord`, `PlanStep`, `RunEvent` (dataclasses, no new DB dependency in the first PR).
- `SkillRouter.execute()` transitions through `planning -> running -> completed / failed / cancelled`.
- `/api/chat` fixed to use `asyncio.to_thread` (not raw `run()` in `async def`).
- Retry policy: `max_attempts=2`, exponential 0.5 s, transient errors only.
- Resume: `RunRecord` captures per-step results; restart resumes from the last completed step without re-running retrieval/reading.
- Events: `RunEvent` dataclass; Christo's persistence schema owns the tables.
- Supersession gate: `CriticAgent` uses confidence thresholds (>=0.80 auto-supersede, 0.50-0.80 contested, <0.50 ignore).
- `verify_citations` gate wired to Adithyan's resolver.

**What does NOT change:**

- `classify_intent()` keyword router (preserve unless measurement shows failure).
- `_EXECUTION_PLANS` mapping.
- `BaseAgent.execute()` contract.
- Existing data keys: `data["answer"]`, `data["cited_nodes"]`, `data["alerts"]`, `data["trace"]`.

**Cost estimate:** 2-3 engineer-days per sprint for 3 sprints (S1-S3). Total: ~6-9 days for Harikesh's deliverables.

**Risks:**
- Resume correctness requires careful state serialization (mitigated: Christo owns storage).
- SSE granularity limited (mitigated: per-step events can be added incrementally).

**Benefits:**
- Zero new dependencies.
- Every acceptance criterion in HARIKESH.md §5 is reachable within the sprint plan.
- Determinism is guaranteed by design (same keyword classifier + frozen plan).
- Green tests stay green; the baseline does not move.
- Fallback is trivially safe: remove the run-state wrapper, revert to the current synchronous `run()`.

---

### Option B — Adopt LangGraph as a wrapper

Introduce LangGraph as a new orchestration runtime wrapping the existing agents.

**What changes:**

- New `[orchestration]` extra in `pyproject.toml` (`langgraph`, `langchain-core`, `AsyncPostgresSaver`).
- `ResearchState` as a `TypedDict`.
- 8-node graph topology: `guard_input -> route -> retrieve -> read -> synthesize -> verify_citations -> check_contradictions -> guard_output -> finalize`.
- `plan` computed once at `route`; repair is an edge counter (max 2), not a replan.
- `AsyncPostgresSaver`, `thread_id = run_id` for checkpointing.
- `SkillRouter` retained as a fallback and for existing tests.
- Christo owns checkpoint tables; Harikesh owns graph nodes and state.

**Cost estimate:** 8-10 engineer-days, distributed across Harikesh + Adithyan + Christo.

**Risks:**
- **Demo risk**: 3 sprints (S1-S3) to build, test, and demo a LangGraph pipeline that does not yet exist in the repo. New failure modes (state shape mismatches, checkpoint schema migrations, interrupt semantics) tend to appear late.
- **Regression risk**: The existing 144+ test suite must pass with LangGraph in the import chain. Library version conflicts are common.
- **Integration overhead**: Adithyan must expose run endpoints; Christo must own Postgres schema and migrations; Abinav must wire the trace UI — all before M2.
- **Deadline risk**: If Option B is not demo-able by M2 (13 Nov), reverting to Option A costs additional days.

**Benefits:**
- Native parallel branches, interrupts, and checkpoint recovery (not needed in the MVP's 4-intent, sequential pipeline).
- Industry-standard library; potential portfolio value.
- Cleaner long-term extension path.

---

## 4. Decision Rule (team-agreed)

> If the team cannot demonstrate Option B **end to end** (create run -> ingest -> ask question -> trace shown -> kill mid-run -> resume) by **M2 (13 Nov 2026)**, revert to Option A and report Option B as a limitation in the demo.

**Do not run both options in parallel.** Do not add LangGraph to `pyproject.toml` before this ADR is marked ACCEPTED.

---

## 5. Recommendation

**Option A.** The MVP definition (§1.1 demo script) requires a single, deterministic, auditable pipeline — not parallel branches or interrupt-driven graphs. The existing `SkillRouter` + `BaseAgent` + `AgentResult` infrastructure already provides the right hooks. Adding a `ResearchRun` record + state machine is 2 days of targeted work per sprint, not a rewrite.

Option B is a correct long-term architecture but is not justified for the MVP given the 3-sprint window and the constraint that the team cannot demo it by M2 without significant risk.

---

## 6. Consequences (if Option A is accepted)

### Immediate (S0 this week):

- [ ] This ADR merged and status changed to **ACCEPTED**.
- [ ] `src/researchos/runs/__init__.py` stub with `RunState`, `PlanStep`, `RunEvent` published.
- [ ] Failure-visibility test plan written in `tests/test_runs.py` (empty tests, stubs only).
- [ ] Team S0 notes document the ownership split.

### Per-sprint (if accepted):

- **S1**: `RunRecord` + state machine + `/api/chat` non-blocking fix (<= 400 changed lines).
- **S2**: Plan generation + optional confirmation gate + retry policy.
- **S3**: Persist/resume; event emission; failure propagation to SSE.
- **S4+**: Supersession policy, `verify_citations` gate, trace completeness.

### If Option B is instead accepted:

- Harikesh publishes `ResearchState`, `RunEvent`, and `PlanStep` as `TypedDict` / dataclass before any node is written.
- No agent is modified until the state shape is agreed.
- `SkillRouter` stays as a test fallback.
- All 14 P0 deliverables are re-evaluated for timeline feasibility.

---

## 7. Approvals

| Person | Role | Decision | Date |
|---|---|---|---|
| Harikesh | Orchestration owner | Proposed A | 25 Oct 2026 |
| Adithyan | API / serving | — | — |
| Christo | Persistence | — | — |
| Abinav | Eval / UI | — | — |

> **To approve:** Add your name, decision (A / B / abstain), and date above and merge a single-line change to this table.

---

## 8. References

- `HARIKESH.md` §6 (this ADR is required before S0 closes)
- `researchos_team_skills/researchos-orchestration/SKILL.md` (skill audit)
- `docs/MVP_IMPLEMENTATION_PLAN.md` §2, §4, §7.3, §7.4
- `src/researchos/agents/router.py` — existing deterministic classifier
- `src/researchos/agents/base.py` — `BaseAgent`, `AgentResult`, `AgentStatus`
- `src/researchos/api/routes/chat.py:54` — blocking `skill_router.run()` in `async def`
