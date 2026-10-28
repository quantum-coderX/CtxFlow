# Christo — Layer 1 Handbook
## Memory Substrate, Isolation & Run State

> **Delivery audit — 25 Oct 2026:** S1–S7 implementation surfaces are present
> and their local tests pass. ResearchOS adapter wiring and the append-only
> `run_events` hash-chain path are complete. The only remaining gate is a live
> staging restore, which requires a separate target database configuration.

| | |
|---|---|
| **Role** | Layer 1 owner: durable, isolated, auditable graph memory and resumable state |
| **Seminar** | *Memory Architectures for LLM-Based Agents* |
| **Primary skill** | [`researchos_team_skills/researchos-ctxflow-memory/SKILL.md`](../../researchos_team_skills/researchos-ctxflow-memory/SKILL.md) |
| **Secondary skill** | None. Read `researchos-orchestration/SKILL.md` only for the run-state contract; do not follow its edit instructions. |
| **Working branch** | `feat/christo-memory` (never commit directly to `Adi/feat/v1`) |
| **Baseline** | `Adi/feat/v1` @ `e1f43c6` (PR #3 merged); `144 passed, 6 skipped` (verified 25 Oct 2026) |
| **Contract freeze** | Fri 30 Oct 2026, 18:00 |
| **Code freeze** | Fri 11 Dec 2026 |

---

## 0. Read this first

1. Read §1–§7 of this file before touching code. They are your contract.
2. Read the primary skill in full. It is the most accurate description of your area.
3. The ownership doc `CODEBASE_LAYERS_AND_OWNERSHIP.md` is **aspirational and partly stale**. The current repository is the truth. `docs/MVP_IMPLEMENTATION_PLAN.md` §7.1 is your delivery contract.
4. Do not start with new architecture. Start by auditing what exists and proving the gap with a failing test.
5. If a contract in §12 of the MVP plan must change, write `docs/adr/NNN-*.md`, get one approval, and change the stub **and** its contract test in the same PR.
6. Anything not in the MVP plan §1 is out of scope. New ideas go to a deferred list, not a branch.

---

## 1. Context in 60 seconds

CtxFlow is a graph-native memory engine for LLM agents. ResearchOS is the application on top of it.

- `CtxFlow` (`src/ctxflow/__init__.py`) is a **synchronous, in-memory** facade over `ContextGraph`, a NetworkX `DiGraph` with an inverted tag index, Jaccard auto-linking, degree governance, drop/summarize pruning, supersession edges, and JSON/MessagePack export.
- `ctxflow.memory` (`src/ctxflow/memory/`) is a **separate async subsystem**: `MemoryStore` protocol, `InMemoryMemoryStore`, `PostgresMemoryStore` (asyncpg + pgvector), `EventIngestor`, `HashEmbedder` / `LocalBGEEmbedder`, `SimpleTokenizer`, plus PR #3 additions `classification.py`, `deduplication.py`, `edge_proposer.py`, `importance.py`, and `ctxflow/extraction_cache.py`.
- `ctxflow/providers/groq.py` is a synchronous `GroqLLM` adapter used for structured extraction and edge proposals. It reads `GROQ_API_KEY` from env/dotenv and is not wired into ResearchOS.
- Migrations: `001_memory_schema.sql` was modified by PR #3; `004_bge_small_384_dimension.sql` moves embeddings to 384-dim BGE and requires `scripts/reembed_bge.py` afterwards.
- Those two memory systems **do not talk to each other**. Nothing in ResearchOS reads or writes `ctxflow.memory`. The API graph is lost on restart.
- ResearchOS today reads and writes only the in-memory `CtxFlow` graph. `project_id` is stored in paper/note metadata, but `CtxFlow.query()` does **not** filter by it; only `GET /api/notes` filters.

Your job is to close exactly that gap: make memory durable, project-isolated, auditable, and resumable, without replacing the working graph engine.

### What already exists (verified at `34cf19f`, re-audited at `e1f43c6` after PR #3)

| Component | State |
|---|---|
| `CtxFlow` facade: `ingest`, `query`, `research_trace`, `prune`, `export`, `import_` | Real and tested |
| `Node`, `Edge`, `CtxFlowConfig`, `QueryResult`, `ProvenanceChain`, `ResearchTrace` | Real; `Node.status` is annotated `str` but assigned `NodeStatus` |
| `ContextGraph`: tag index, BFS, `trace_provenance`, degree helpers | Real |
| Governance and pruning | Real; degree cap is uneven across edge-write paths |
| JSON / MessagePack serialization | Real; schema `version: 1` |
| `ctxflow.memory` stores, ingestion, embeddings, tokens, runtime | Real but **unwired**; PR #3 added classification, dedupe, edge proposals, importance, and an extraction cache |
| `record_access` on the `MemoryStore` protocol | **Required** as of PR #3; implemented by both built-in stores, breaks third-party stores |
| Local BGE embeddings (`LocalBGEEmbedder`, 384-dim) + `scripts/reembed_bge.py` | Real; `HashEmbedder(384)` fallback when `sentence-transformers` is absent |
| Groq adapter (`ctxflow/providers/groq.py`) | Real, synchronous; no ResearchOS wiring |
| `project_id` on memory records | Implemented in stores, migration `005`, and isolation tests |
| PostgreSQL persistence for ResearchOS | Memory store/runtime implemented; ResearchOS app wiring remains outstanding |
| Run/checkpoint/event tables | `run_events` table exists in migration `005`; append-only hash-chain writer remains outstanding |
| Background compaction job / CLI | Implemented in `src/ctxflow/maintenance.py` and `scripts/compact_graph.py` |
| 10 000-node performance benchmark | Implemented in `scripts/bench_graph.py`; measured within target |

---

## 2. Your mandate

> Make CtxFlow the durable, isolated, auditable memory that the orchestration layer reads and writes.

Three properties, in this order:

1. **Isolation** — a query for project A can never return project B's nodes, even if a caller forgets a filter. Fail closed.
2. **Durability** — a process kill must not lose committed memory or leave half-applied truth-maintenance writes.
3. **Auditability** — every write that changes belief leaves an append-only, replayable event.

Performance is fourth: query P95 < 150 ms at 10k nodes, ingest P95 < 50 ms.

---

## 3. Skill assignment (what to load and why)

### Load this

`researchos_team_skills/researchos-ctxflow-memory/SKILL.md`

It defines:
- the `ContextRecord` / `ContextProvider` adapter contract,
- the project-scope decision,
- token-budgeted selection using `SimpleTokenizer`,
- supersession/history handling,
- parity and isolation acceptance criteria.

### Do not load as instructions

- `researchos-evidence-retrieval` — source parsing is Adithyan's.
- `researchos-orchestration` — run lifecycle and router are Harikesh's.
- `researchos-frontend-integration-evaluation` — UI and evaluation are Abinav's.

You may read the other skills to understand the contracts you consume. Do not edit their files.

### Conflict to resolve by S0

The skills assignment (commit `6ae8b0f`) makes you memory owner. The older layer doc also names Abinav/Adithyan for context and serving. That does not change your files here: you own `src/ctxflow/` core plus `src/ctxflow/memory/`, migrations, and scripts. The `ContextProvider` adapter lives in `src/researchos/memory/context.py`; create it with Harikesh's agreement because it sits on the orchestration boundary.

---

## 4. Ownership map

### You own — write access

| Path | What you are responsible for |
|---|---|
| `src/ctxflow/models.py` | `Node`, `Edge`, `CtxFlowConfig`, `QueryResult`, provenance/trace dataclasses; status representation |
| `src/ctxflow/graph.py` | `ContextGraph`, tag index, BFS, `trace_provenance` (coordinate with Harikesh) |
| `src/ctxflow/governance.py` | Degree caps, weight/score normalization |
| `src/ctxflow/pruning.py` | Staleness, drop/summarize compaction |
| `src/ctxflow/serialization.py` | JSON/binary persistence, schema versioning |
| `src/ctxflow/memory/**` | `store.py`, `inMemorystore.py`, `postgres_store.py`, `ingestion.py`, `tokens.py`, `embeddings.py`, `runtime.py` |
| `migrations/**` | New numbered SQL migrations only |
| `scripts/**` | `bench_graph.py`, backup/restore, maintenance entry points |
| `tests/test_graph.py`, `test_governance.py`, `test_pruning.py`, `test_serialization.py`, `test_models.py`, `test_memory_vertical_slice.py`, `test_postgres_runtime.py`, `test_postgres_integration.py` | Your regression surface |
| `src/researchos/memory/context.py` (new) | The narrow, optional adapter to `MemoryStore` |

### Shared — edit only with the named owner in the PR

| Path | Coordinate with | Why |
|---|---|---|
| `src/ctxflow/__init__.py` | Harikesh | `research_trace()` facade and post-ingest hooks |
| `src/ctxflow/contradiction.py` | Harikesh | Atomic supersession transaction |
| `src/researchos/orchestration/state.py` (if created) | Harikesh | `RunEvent`, `project_id`, checkpoint keys |
| `src/researchos/api/schemas.py` | Abinav | New request/response fields |
| `src/researchos/api/server.py` | Harikesh / Abinav | Wiring the adapter behind a flag |
| `pyproject.toml` | Adithyan | Dependencies and extras |
| `tests/contract/**` | All | Frozen interfaces |

### Read-only for you

`src/researchos/skills/**`, `src/researchos/agents/**` (except `__init__.py` wiring), `src/researchos/web/**`, `src/researchos/eval/**`.

---

## 5. P0 deliverables

Each item is done only when its acceptance test exists and passes.

| # | Deliverable | Acceptance |
|---|---|---|
| 1 | Add `project_id` to memory records and the schema; every retrieval path filters by it | Cross-project leakage test fails closed; `project_id` required at the API boundary |
| 2 | Wire an optional `ContextProvider` adapter over `MemoryStore` | ResearchOS runs with memory disabled; no import errors; flag controls enablement |
| 3 | Demote `record_access` (made required by PR #3) to an optional capability, alongside `add_edge`, `add_provenance`, `claim_event`, `persist_ingestion`; call it via `getattr` | Both built-in stores pass the same contract tests; a third-party store without `record_access` still works |
| 4 | Atomic supersession write: edge insert + `status` + `superseded_by` in one transaction | Fault-injection test leaves **no** half-applied state |
| 5 | Enforce degree caps on **every** edge-write path, not just the new node's outgoing side | Property test: 1 000 random inserts, max outgoing degree respected |
| 6 | Compaction is a maintenance job, never inline with a user query | Demo query latency unaffected by pruning; CLI documented |
| 7 | Append-only `run_events` with per-run hash chain | Tamper test detects a broken chain; event survives restart |
| 8 | Retention/backup policy for checkpoints and events | Run migration twice (idempotent); backup/restore script works |
| 9 | Performance budget | `scripts/bench_graph.py` output committed: query P95 < 150 ms @ 10k nodes, ingest P95 < 50 ms |
| 10 | Fix the in-memory/Postgres parity bug | `InMemoryMemoryStore.search()` honours `include_history` and filters `superseded` like Postgres; parity test |

### Skill-level acceptance (must also hold)

- Context records carry stable IDs, provenance (`source_type`, `source_id`), validity, token estimate, and score.
- Token budgeting uses the documented estimator, not `len(text) // 4`.
- Superseded records are never returned as current truth; history is available explicitly.
- Selected/excluded records, scores, and token estimates are inspectable.
- Tests cover empty project, exact-budget edge, duplicate, supersession, isolation, persistence (Postgres test marked `postgres`).
- The app works with the adapter disabled.

---

## 6. Implementation order

1. **Audit and baseline.** Read `store.py`, `inMemorystore.py`, `postgres_store.py`, `ingestion.py`, `tokens.py`, `runtime.py`, the migration, and `src/researchos/agents/reader.py`, `api/routes/papers.py`, `api/routes/notes.py`.
2. **Decide the project key** (S0, documented in the PR):
   - Option A: `session_id` as project key. Smallest change, no migration, but conflates chat sessions with projects. State the limitation explicitly.
   - Option B: add `project_id` to `Memory` + `memories` table + `search()` + `EventIngestor`. Correct model, needs `migrations/002_memory_scope.sql`. **Recommended.**
   - Never fetch everything and filter in Python. That is not isolation and will not scale.
3. **Write the failing tests first**: isolation, parity, token budget, atomic supersession, idempotent migration.
4. **Add the migration** as a new file. `001_*.sql` was modified by PR #3 and must not be edited again; `004` is taken. Use `005+`.
5. **If you touch embeddings**, read `migrations/004_bge_small_384_dimension.sql` and `scripts/reembed_bge.py` first. Migration 004 clears vectors before re-embedding; back up and rehearse it, never run it casually.
6. **Implement store support** and the adapter. Keep `ContextProvider` optional.
7. **Map artifacts to memory types**: paper → `DOCUMENT`/`DOCUMENT`; claim/evidence → `FACT`/`OBSERVATION` with `DERIVED_FROM`; note → `OBSERVATION`/`USER_MESSAGE`; decision → `DECISION`.
8. **Rank and pack**: relevance + explainable signals (validity, source type, confidence); deterministic order; stop at budget; record exclusions.
9. **Handle supersession** through `ValidityState` + `Relation.SUPERSEDES` + `create_version()`. Preserve history.
10. **Wire behind a flag** in `create_app()` only after Harikesh/Abinav agree.
11. **Add run-state persistence** (see §7).
12. **Benchmark and document**: graph stats, memory footprint, migration steps, limitations.

---

## 7. Run state: what is yours and what is not

The MVP plan promotes LangGraph and `AsyncPostgresSaver`; the team skills say LangGraph does not exist and must not be added without justification. **This is an open ADR (see Harikesh's handbook, ADR-001).** Until it is accepted:

- Do **not** add LangGraph to `pyproject.toml`.
- Do **not** create an `orchestration/` package.

Regardless of runtime choice, these are yours:

- The `run_events` table and append-only write path (G11).
- `project_id` scoping for every persisted artifact.
- Checkpoint table migrations and retention, if checkpoints are approved.
- Atomicity of write paths (memory + run event in one transaction where they must agree).

If ADR-001 is accepted, you own the **storage** half (`asyncpg` schema, migrations, retention, resume-after-crash test); Harikesh owns the **control plane** (graph topology, node wiring). Do not let that boundary blur.

---

## 8. What NOT to do

Hard prohibitions. A PR that does any of these is rejected.

1. **Do not add required methods to the `MemoryStore` protocol.** PR #3 did this and broke third-party stores. Add optional capabilities and detect with `getattr`.
2. **Do not edit an existing migration in place.** Add a numbered migration. `001_memory_schema.sql` is history.
3. **Do not build a second vector database, embedding service, or ORM.** `pgvector` and `asyncpg` already exist.
4. **Do not replace `CtxFlow` with `MemoryStore`, or vice versa.** The adapter is the seam.
5. **Do not silently filter project data in Python** after fetching everything.
6. **Do not present Postgres keyword-only search as hybrid retrieval.** It returns `score = 0.0` and orders by `created_at`.
7. **Do not change scoring weights or `recency` semantics unilaterally.** `query.py` is the context owner's file; a change there needs their review and a benchmark.
8. **Do not add heavyweight dependencies.** Core stays `networkx`, `asyncpg`, `pydantic`, `dotenv`.
9. **Do not run compaction inline** on the query path.
10. **Do not claim persistence works** until a restart test proves it, and a Postgres test is green in a live environment.
11. **Do not edit the router, EvidenceCard, prompts, UI, or evaluation harness.** Request the change from the owner.
12. **Do not write a migration that is not idempotent** or that drops user data.
13. **Do not leave `project_id` optional** in a new write path. Missing project scope is a bug, not a default.
14. **Do not mix unrelated refactors into a contract PR.** PRs stay ≤ 400 changed lines.

---

## 9. Contracts you publish / consume

### You publish by S0 Friday

```python
# src/researchos/memory/context.py (proposed shape; publish it before S1)
@dataclass(frozen=True)
class ContextRecord:
    memory_id: str
    text: str
    tokens: int
    memory_type: str
    validity: str
    score: float
    source_type: str | None = None
    source_id: str | None = None
    superseded_by: str | None = None

class ContextProvider(Protocol):
    async def retrieve(
        self,
        project_id: str,
        query: str,
        token_budget: int,
    ) -> list[ContextRecord]: ...
```

Also publish:
- `MemoryStore` protocol and its optional capabilities.
- `project_id` convention (where it is stored, who sets it, what happens when absent).
- `RunEvent` persistence schema and hash-chain rule.

### You consume

| From | Contract |
|---|---|
| Harikesh | `RunEvent`, run-state keys, trace requirements |
| Adithyan | evidence/source IDs and `access_status` (so memory records can point to real sources) |
| Abinav | `EvidenceCard` / `Claim` shapes, token-budget expectations |
| All | `tests/contract/` fixtures |

---

## 10. Sprint plan (yours only)

| Sprint | Deliverable | Gate |
|---|---|---|
| **S0** 26–30 Oct | Audit; project-key decision; publish `ContextProvider` + `MemoryStore` contract; checkpoint/event table plan; freeze `MemoryStore` | Contracts frozen |
| **S1** 2–6 Nov | `project_id` migration; store support; parity fix; `run_events` skeleton | Isolation test fails closed |
| **S2** 9–13 Nov | Atomic supersession transaction; degree-cap property test | Fault injection: no half-write |
| **S3** 16–20 Nov | Adapter + token-budget packing wired behind flag; retention policy | App runs with memory on and off |
| **S4** 23–27 Nov | Perf pass at 10k nodes; `scripts/bench_graph.py`; compaction job | P95 targets met or variance explained |
| **S5** 30 Nov–4 Dec | Migration idempotency; isolation tests; backup/restore script | Restore rehearsal |
| **S6** 7–11 Dec | Demo corpus ingest; graph stats; docs; memory footprint | Numbers reproducible |
| **S7** 14–18 Dec | Backup/restore rehearsal; rollback plan | Frozen build |

### Sprint verification status

| Sprint | Status | Evidence |
|---|---|---|
| S0 | Complete | Adapter contract, project-scope convention, and feature-flag boundary |
| S1 | Complete | Project migration, store scoping, parity coverage, and `run_events` schema |
| S2 | Complete | Atomic supersession and degree-cap regression coverage |
| S3 | Complete | Optional adapter factory, token packing, retention, and `create_app()` wiring |
| S4 | Complete | 10k benchmark and offline compaction CLI; P95 targets measured below budget |
| S5 | Complete with rehearsal limitation | Idempotent migration, isolation tests, backup/restore CLI; archive validated with `pg_restore --list`, staging restore not executed |
| S6 | Complete | Checked-in demo corpus, reproducible graph statistics, footprint report, and documentation |
| S7 | Partial | Rehearsal/rollback tooling and event-chain verification are complete; live staging restore remains pending |

---

## 11. Definition of Done

A deliverable is done when **all** are true:

- [x] A test covers the happy path **and** one failure path for delivered S1–S7 surfaces.
- [x] `python -m pytest -q` is green: 172 passed, 5 skipped.
- [x] New migration is additive/idempotent by construction and covered by tests.
- [x] No new required method on a shared protocol.
- [x] External provider calls retain configured timeout/retry boundaries.
- [x] `project_id` is enforced on the delivered memory-store paths.
- [x] Optional append-only `run_events` writer and hash-chain verification are implemented and tested.
- [x] Docs updated with verified status and limitations.
- [x] Performance and footprint results are recorded.
- [ ] Live staging restore has been executed and verified; no separate staging configuration is present.

### Commands you must run and report

```bash
python -m pytest -q
python -m pytest -q tests/test_graph.py tests/test_governance.py tests/test_serialization.py tests/test_memory_vertical_slice.py tests/test_postgres_runtime.py
python -m pytest -q tests/test_postgres_integration.py -m postgres   # live DB only
python -m researchos.eval                                            # must still work
```

---

## 12. Known landmines in your area

| Landmine | Consequence | Your action |
|---|---|---|
| `Node.status` is `str` but assigned `NodeStatus` | Consumers do defensive `isinstance` checks | Pick one representation, migrate call sites, test both old and new data |
| `ContextGraph.add_edge` overwrites `(source, target)` | Two relations between the same pair cannot coexist; weights silently replaced | Document it; if you need both, change the model with an ADR |
| Degree cap only checks the new node's out-degree | Uneven enforcement; hubs can still form | Enforce centrally in the edge-write path |
| `prune("drop")` has no edge healing | Graph fragments | Document; do not promise healing |
| `prune("summarize")` returns only removed IDs | Callers cannot find summary nodes | Return both, or add a separate result type |
| `bfs_distances` is undirected | Proximity ignores edge direction | Keep, but state it in the contract |
| `recency = 1/(1+age_seconds)` | Almost every node scores ~0 | Do not "fix" silently; coordinate with context owner |
| `embedding_enabled` / `embedding_model` / `prune_min_access` are dead config | False expectations; the model default is now BGE but the facade still never reads it | Remove or wire; do not document them as features |
| `record_access` is a **required** protocol method after PR #3 | Any third-party `MemoryStore` without it breaks | Demote to optional capability and call via `getattr` |
| `InMemoryMemoryStore.search()` ignores history and supersession | Divergence from Postgres | Fix now; add parity test |
| Migration `004` nulls vectors before re-embedding | Running it without a backup/re-embed loses retrieval quality | Back up, run `scripts/reembed_bge.py`, verify counts |
| Route modules hold module-level `_app_state` | Two apps collide; not task-safe | Do not build run state on it |
| `migrations/001` is not auto-run and was modified by PR #3 | Fresh clone has no tables; old copies diverge | Document setup; add a check at startup or a script; never edit `001` again |

---

## 13. Coordination and escalation

| Topic | Person | Rule |
|---|---|---|
| Run state, events, trace | Harikesh | Publish schema first; he consumes it |
| Evidence/source IDs, page metadata | Adithyan | Memory points to sources by stable ID |
| API fields for memory status | Abinav | One schema change, all consumers updated |
| New dependency | Adithyan | Justify measured need; extras only |
| Contract change after 30 Oct | All | ADR + approval + contract test |

**Escalate immediately if:** a migration would rewrite existing data, an isolation test cannot fail closed, or a teammate needs a required protocol method.

---

## 14. Copy-paste kickoff prompt

```text
You are Christo, Layer 1 owner for ResearchOS in D:\DEEPLEARNIING\CtxFlow.

Read, in this order:
1. docs/team/CHRISTO.md
2. researchos_team_skills/researchos-ctxflow-memory/SKILL.md
3. docs/MVP_IMPLEMENTATION_PLAN.md sections 1, 4, 7.1, 9 (G2, G11), 12, 13
4. REPO_HANDOFF_SUMMARY.md sections 3, 4, 10, 11
5. The files you own, especially src/ctxflow/memory/* and migrations/001_memory_schema.sql

Then run `python -m pytest -q` and report the real count. Do not change code yet.

Your first task: audit the project-scope gap and publish the ContextProvider + MemoryStore
contract. Do not add required methods to MemoryStore. Do not edit 001 SQL. Do not add LangGraph.
Do not touch router, prompts, UI, or eval files. Report exact commands, results, and limitations.
```

---

## 15. One-screen summary

- **Own:** `ctxflow` core + `ctxflow.memory` + migrations + scripts.
- **Ship:** project isolation, atomic supersession, run events, optional context adapter, perf numbers.
- **Never:** required protocol methods, in-place migrations, second vector DB, Python-side isolation, unverified persistence claims.
- **Skill:** `researchos-ctxflow-memory` only.
- **Done means:** tests green, migration idempotent, restart/resume proven, numbers committed.
