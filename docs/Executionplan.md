# CtxFlow / ResearchOS: Grounded System Design and Execution Plan

**Status:** Working plan; architecture is based on the checked-out code, not a claim
that the current branch is production-ready.  
**Verified:** 23 November 2026  
**Branch / HEAD:** `feat/adithyan-evidence` / `568de7e`  
**Authoritative for:** Implementation sequencing from the current branch to the
complete research workstation. Product vision and principles remain in
[`read.md`](read.md).

## 1. Executive decision

**End product:** a complete research workstation. A user creates a project
workspace, discovers and ingests literature, builds evidence-backed memory,
analyses datasets, produces charts, and generates publication-ready documents
(including a LaTeX/PDF engine) with every claim traceable to a source or dataset
version. Phases P0-P8 below build all of it, in dependency order.

Build it as a modular monolith, one validated layer at a time. Each phase has an
exit gate and later phases do not start on the assumption that an earlier gate
passed. The first deliverable is one reliable research-memory vertical slice:

> User request → bounded research workflow → retrieved papers/evidence → optional
> project-scoped CtxFlow memory → evidence-grounded synthesis → citation checks →
> persisted run and trace.

Keep the system as a **modular monolith**: FastAPI application, CtxFlow memory
library, ResearchOS workflow and skills, and the existing project workspace
boundary. Keep Laya and model providers behind adapters. Do not introduce
microservices, Redis, a graph database, or a new persistence backend until a
measured requirement makes one necessary.

Use the existing LangGraph engine when installed and retain the sequential engine
as an explicit fallback. Treat its current `MemorySaver` as **process-local
checkpointing for inspection**, not crash recovery or durable resume. Do not
promise restart/resume until a persistent checkpointer and an end-to-end restart
test exist.

Keep durable-memory retrieval disabled unless the application is explicitly
given a store and `RESEARCHOS_MEMORY_ENABLED` is enabled. Keep Laya selection
opt-in with `LAYA_CONTEXT_SELECTION`; the deterministic heuristic router is an
availability fallback, not evidence that the learned Laya model was evaluated.

## 2. Verified repository status

### Checkout

- The working tree is not clean. `README.md` and multiple application files are
  modified; several documentation files are deleted; new Laya and planning
  files are untracked. These are existing user changes and must be preserved.
- `docs/ctxflow_s7_rollback.md` and `docs/team/{ABINAV,ADITHYAN,CHRISTO,HARIKESH}.md`
  are deleted in this worktree. Some current documentation still points to them.
- Do not treat commit `568de7e` alone as the implementation being assessed, and
  do not restore or discard those worktree changes without an explicit request.

### Validation performed for this assessment

```text
python -m pytest -q tests/test_laya_context.py \
  tests/test_context_provider_wiring.py tests/test_langgraph_workflow.py
21 passed
```

This is a focused integration check, not a full-suite result. No live database,
provider, restore, or production deployment was exercised.

### Current implementation, from code and tests

| Area | Verified behavior | Boundary / remaining proof |
|---|---|---|
| API and workspace | FastAPI `create_app`; projects use `ResearchWorkspace`; `/api/stream` executes a research run, emits events, and saves the run result. | Verify deployment filesystem/database durability and restore procedure separately. |
| Orchestration | Shared eight-stage workflow: `plan → retrieve → read → memory → pack → synthesize → verify → persist`. LangGraph is preferred unless `RESEARCHOS_ORCHESTRATOR=sequential` or LangGraph is unavailable. | ADR-001 still says proposed and forbids adding LangGraph before approval; this conflicts with the shipped dependency and default. |
| Graph checkpoint | `ResearchGraph` creates `MemorySaver`/`InMemorySaver` by default, keyed by `run_id`. | In-memory only; not a persistent resume guarantee across process restart. |
| Durable memory | `create_app` accepts an injected `memory_store`; provider creation is opt-in through `RESEARCHOS_MEMORY_ENABLED`. When enabled without an injected store, the lifespan now builds and closes `PostgresMemoryRuntime` and exposes `app.state.memory_status`. | Live PostgreSQL wiring has only been exercised with a fake runtime in tests; verify against a real staging database in P3. |
| Context selection | Greedy provider by default; `LAYA_CONTEXT_SELECTION` selects `LayaMemoryContextProvider`. Project ID, task profile, required skills, prior context IDs, and a fraction of run token budget are passed into retrieval. | Laya quality/latency on representative labeled queries is not established by mocked/heuristic tests. |
| Degraded memory | Memory lookup failure adds an alert and allows the research run to continue. | Keep degraded status visible to clients and operators; do not silently interpret it as a successful memory lookup. |
| Evidence and reasoning | The workflow packs graph/evidence context, calls structured synthesis, verifies citations, and exposes result/event fields. Memory is additional background, not a citable source. | Measure citation support and source coverage on a fixed evaluation set. |
| Acquisition | Literature search, arXiv MCP with HTTP fallback, web search, source fetching, paper reading, and a checked-in demo corpus exist. | Provider outages, licensing, source-version provenance, and ingestion quality need explicit release checks. |
| Tool harness | `SkillRegistry` validates typed skill inputs; `ToolHarness` validates a bounded task DAG, runs independent registered skills concurrently, passes outputs only through declared references, and records completed/failed/blocked outcomes. The workstation engines below are registered as allowlisted skills, so a plan such as ingest → analyze → chart → compose runs through the harness. | This is an internal execution seam; it is not exposed as an arbitrary-plan HTTP API. No model-supplied code is evaluated. |
| Workstation engines | `researchos.workstation.Workstation` bundles: versioned datasets (`analytics.py`), a fixed 13-operation statistics catalog and 9 validated transforms, `create_chart(spec)` (`charts.py`), a project source registry with stable BibTeX keys (`sources.py`), document specs → Markdown/HTML/LaTeX/BibTeX with isolated Tectonic/pdflatex compilation, staleness and refresh-rebuild (`artifacts.py`), stored PDFs with page-located text/OCR (`ocr.py`), and a bounded Lean adapter (`proof.py`). REST routes live in `api/routes/datasets.py` and `api/routes/artifacts.py`; the UI has Data and Reports tabs. | XLSX/Parquet need `openpyxl`/`pyarrow`; PDF needs Tectonic or pdflatex; OCR needs Tesseract or Nougat; proof checking needs Lean. Missing tools report `unavailable`. GROBID/Docling, Crossref, DuckDB SQL, Plotly and Pandoc DOCX are not implemented. Lean and Nougat adapters are tested with stubbed subprocesses only. |
| Vision scope | [`read.md`](read.md) describes the full workstation: claim-level evidence, analytics, visualization, artifacts, and independent workspaces. | See the P5-P8 progress notes below for what is implemented and what remains. |
| Laya role | Laya can be selected behind `LAYA_CONTEXT_SELECTION` for memory-context decisions; greedy selection remains the default. | The learned model has not been evaluated on labeled ResearchOS tasks. The current adapter does not establish a general coordinator/router or a correctness judge; keep orchestration deterministic and use Laya only for validated structured decisions until measured. |

### Planning/documentation conflicts to resolve

1. `IMMEDIATE_NEXT_STEPS.md` does not exist in this checkout. Its Laya wiring and
   System 2 integration work is already implemented in both engines; the
   remaining evaluation/runbook work is tracked in P2 and P3 here.
2. `docs/adr/001-orchestration-runtime.md` does not exist in this checkout. P0
   creates it as ACCEPTED, recording LangGraph as the default with the
   sequential fallback.
3. The root README no longer links to the deleted restore runbook and team
   notes (resolved in P0). A real rollback runbook is still required by P3.
4. The root README’s Phase 1 and restore statements are dated claims. Confirm
   them against current code and staging evidence before representing them as
   current operational status.

## 3. Target system design

### 3.1 Logical architecture

Keep these boundaries inside the existing Python application for the near-term
release:

```text
Browser / API client
        |
        v
FastAPI routes (validation, auth boundary, SSE, run records)
        |
        v
Research workflow (LangGraph default | sequential fallback)
  plan -> retrieve -> read -> memory -> pack -> synthesize -> verify -> persist
    |         |         |       |       |          |           |
    |         |         |       |       |          |           +-- citation/evidence checks
    |         |         |       |       |          +-------------- System 2 gateway
    |         |         |       |       +------------------------- budgeted context assembly
    |         |         |       +--------------------------------- optional CtxFlow memory adapter
    |         |         +----------------------------------------- paper/source reading
    |         +--------------------------------------------------- graph and external retrieval
    +------------------------------------------------------------- deterministic task plan
        |
        +--> ResearchWorkspace: project graph, runs, conversations, UI data
        +--> injected MemoryStore: scoped durable memories, provenance, versions
        +--> provider adapters: arXiv/MCP + HTTP, web, model gateway
```

The diagram distinguishes two stores that must not be conflated:

- **ResearchWorkspace** owns project-facing research state (project graph, run
  records, conversation/history and associated workspace persistence).
- **CtxFlow `MemoryStore`** owns durable project-scoped memory records,
  versions, provenance, and retrieval. ResearchOS consumes it through
  `ContextProvider`; the store remains the authority for memory writes.

The LangGraph checkpointer is a third, separate concern. Its current default is
process memory and does not replace either store.

### 3.2 Ownership and contracts

| Component | Owns | Contract / invariant |
|---|---|---|
| API routes | Request bounds, project/session identifiers, run lifecycle, SSE, user-visible errors | Never report a provider outage or failed persistence as an ordinary successful result. |
| Workflow state | Per-run typed inputs, stage outputs, alerts, events and status | Both engines return the same result shape and emit the same stage semantics. |
| Research skills | Search/fetch/read integrations and provider-specific outcome detail | Preserve `ok / empty / partial / error` distinctions; do not flatten provider errors into an empty successful result. |
| CtxFlow store | Memory records, scope, validity, versions, provenance and durable memory operations | Retrieval always applies project scope; superseded/invalid records are not reintroduced as valid context. |
| Context provider | Candidate retrieval, selector choice, token accounting and `ContextRecord` construction | Must honor the supplied token budget and return IDs/reasons/scores/provenance as metadata. |
| Laya adapter | Structured relevance/validity/continuity/importance decisions | Makes decisions only; deterministic code validates and packs. It does not write memory or author evidence. |
| Model gateway | Provider selection, bounded calls/retries, usage and errors | Provider-independent interface; failures remain visible and bounded. |
| ResearchWorkspace | Project graph and run-facing persistence | Persists the run outcome and trace separately from LangGraph's temporary checkpoint. |

### 3.3 Run and context data flow

1. API validates query, project, session and requested budget; creates a unique
   `run_id` and records the incoming run.
2. Workflow creates `ResearchState` and deterministically plans bounded
   retrieval/read tasks.
3. Internal graph retrieval and external search/read return source-bearing
   evidence and provider outcome metadata.
4. If durable memory is enabled and a store was injected, the `memory` stage
   requests at most the configured share of the run token budget. It passes
   `project_id`, query, task profile, required skills and previous-turn memory
   IDs to the provider.
5. Greedy or Laya selection returns `ContextRecord`s. Keep memory records
   labeled as background; they do not become citation evidence merely by being
   included in the prompt.
6. Pack graph evidence plus bounded background memory; invoke structured
   synthesis through the model gateway or deterministic path.
7. Verify citation IDs against available evidence. A bounded repair may repack
   and re-synthesize; it must not loop indefinitely.
8. Persist final run, events, answer, status, engine, alerts and context
   metadata. Return the same outcome to SSE/client consumers.

### 3.4 State, persistence and recovery

- **Run state:** one run ID; explicit status (`queued`, `running`, terminal
  status); stage/event sequence; provider outcomes; answer/citations; selected
  memory IDs; engine/selector; timestamps and errors.
- **Research data:** persist through the existing `ResearchWorkspace` and
  existing CtxFlow storage adapters. Do not add a second authority for graph
  nodes or memories.
- **Checkpoints:** current `MemorySaver` is adequate for in-process test/demo
  inspection. Persistent checkpointing is a separate design and migration
  decision; until implemented and tested, expose no “resume after restart”
  capability.
- **Replay:** use recorded inputs, provider outcomes where feasible, model and
  selector names, and run events. Avoid claiming deterministic replay of live
  external search or stochastic model output.
- **Failure semantics:** missing optional memory may degrade a run and continue;
  failed final run persistence must surface explicitly. Provider failure must
  remain distinguishable from a valid empty search.

### 3.5 Security, scope and trust boundaries

- Enforce project scope at every memory lookup and verify no cross-project
  leakage with negative tests.
- Treat fetched pages, papers, MCP output, and extracted text as untrusted input.
  Keep them as data; do not let prompts grant them tool/database authority.
- Keep secrets in environment/deployment configuration, not checked-in docs,
  tests, or fixtures.
- Keep model and System-1 outputs as proposals. Validate tool requests, IDs,
  citations, budgets, and persistence mutations in deterministic code.
- Do not enable arbitrary code execution or unrestricted filesystem access.
  Analytics (P7) runs only a fixed, validated set of operations, and LaTeX
  compilation (P6) runs in an isolated, time-limited subprocess.

### 3.6 End-state workstation architecture

The target keeps the same modular monolith and adds engines behind validated
interfaces. Models and Laya produce structured requests; deterministic engines
validate and execute them.

```text
Research Workspace (project: sources, datasets, claims, artifacts, history)
  |
  +-- Acquisition      arXiv / Semantic Scholar / OpenAlex / Crossref / web   (exists, P3-P5)
  +-- Ingestion        PyMuPDF, GROBID or Docling -> sections, refs, tables   (P4-P5)
  +-- CtxFlow memory   claims, provenance, supersession, scoped retrieval     (exists, P1-P4)
  +-- Laya (System 1)  structured relevance / ranking decisions               (P2)
  +-- System 2         synthesis over packed evidence                         (exists)
  +-- Artifact engine  document spec -> Jinja2 LaTeX -> BibTeX -> Tectonic PDF (P6)
  +-- Analytics        DuckDB / Pandas / SciPy / Statsmodels, fixed operations (P7)
  +-- Visualization    Matplotlib / Plotly via create_chart(spec)             (P7)
  +-- Workspaces       per-project isolation, artifact versions, regeneration (P8)
```

Engine contracts:

| Engine | Input | Output | Invariant |
|---|---|---|---|
| Ingestion | PDF/HTML/DOCX/CSV source + source identity | Sections, references, tables, metadata, proposed claims | Extracted claims are proposals with origin (`author_reported`, `system_inferred`, `verified`) and review status |
| Artifact | Document spec (sections, claim/source IDs, tables, figures, equations) | `.tex`, `.bib`, PDF, DOCX/HTML | A build fails on any unresolved citation ID; escapes all untrusted text before templating |
| Analytics | Dataset version + named operation + parameters | Result table, parameters, dataset version | The model requests an operation; it never computes or writes executable code |
| Visualization | Dataset version + chart spec | Image, chart metadata, data used | Chart data equals the stored dataset version |

## 4. Architecture decisions (recommended defaults)

These are the proposed system decisions for team ratification; no production
guarantee should be inferred before the corresponding gate passes.

| Decision | Recommended default | Why / explicit boundary |
|---|---|---|
| Deployment shape | Modular monolith, existing FastAPI app | Fits current code and team; no measured need for distributed services. |
| Workflow runtime | Accept current LangGraph default; retain sequential compatibility mode | Already implemented and tested; preserve the common stage/result contract. Record this in ADR-001 and remove its now-false pre-approval constraint. |
| Checkpoint persistence | Do not add a Postgres checkpointer in this plan’s first release | Current demo and run-record path exist; durable resume adds schema, lifecycle, locking, and recovery obligations. Revisit only if restart/resume is a product acceptance criterion. |
| Memory DB lifecycle | Lifespan builds and closes `PostgresMemoryRuntime` when memory is enabled and no store is injected; an injected store takes precedence; startup failure runs without memory and is visible in `app.state.memory_status` | Implemented in P1; keeps the pool on the app loop, which the workflow reaches through `memory_loop`. |
| Selector rollout | Greedy default; Laya behind flag; heuristic fallback clearly reported | Prevents unmeasured ranking behavior from silently changing the default. |
| Evidence policy | Only source-backed research graph records can satisfy citations; memory context is non-citable background | Keeps continuity useful without confusing memory with evidence. |
| Data services | PostgreSQL/pgvector for durable memory where configured; NetworkX/workspace graph as implemented | Reuse existing adapters and migrations; avoid Redis or a separate graph DB until justified. |
| Product scope | Full research workstation, built in order P0-P8 | Foundation (P0-P4) first; then ingestion, LaTeX artifact engine, analytics/visualization, workspaces. |
| LaTeX toolchain | Tectonic, with Pandoc for DOCX/HTML and Jinja2 templates | Single binary, automatic package fetching, simple on Windows. Fall back to a full TeX distribution only if a required package is unavailable. |
| Build order P6 vs P7 | Artifact/LaTeX engine before analytics | A literature review needs no datasets, so reports are useful earlier; figures and tables integrate when P7 lands. |
| Analytics execution | Fixed, validated operation set; no model-written code | `read.md` requires models to request computations. Sandboxed model-written code is a separate later decision. |
| Document sources | Reports are generated from structured specs, not free LLM text | Keeps citations resolvable and reports regenerable when evidence changes. |

## 5. Execution plan

Tasks are ordered by dependency. Owners are intentionally role-based until the
team confirms assignments. Do not start a later phase by assuming an earlier
gate passed.

### P0 — Make the current branch releasable

**Goal:** Know exactly what is being merged and align the docs with the shipped
design.

1. Review every existing modified/deleted/untracked file in the branch; preserve
   intentional work and identify accidental changes. Do not use blanket restore
   or cleanup commands.
2. Run the full test suite from the final intended worktree and record the
   command/result. Resolve only failures caused by this branch.
3. Create `docs/adr/001-orchestration-runtime.md` as ACCEPTED: LangGraph is the
   default, the sequential engine is the fallback, and `MemorySaver` is
   process-local inspection, not durable resume. **Done**
   ([ADR-001](adr/001-orchestration-runtime.md)).
4. Remove README links to deleted docs/team notes. **Done**; a real rollback
   runbook is still needed before the P3 restore claim.
5. Correct stale references in this document (`adr/read.md` is `read.md`;
   `IMMEDIATE_NEXT_STEPS.md` and ADR-001 did not exist). **Done** for this
   document.

Note: three script tests (`demo_corpus`, `compact_graph`, `bench_graph`) fail
unless `PYTHONPATH=src` is set or the package is installed with `pip install -e .`,
because they run scripts in subprocesses. The suite passed with `PYTHONPATH=src`
before the P1 changes.

**Exit gate:** full tests pass; the branch’s file changes are accounted for;
architecture decision and all README links/status claims are internally
consistent.

### P1 — Prove safe memory wiring end to end

**Goal:** Make the optional store lifecycle and its failure behavior deployable.

1. Decide and document who constructs the PostgreSQL memory runtime and who
   closes its pool. Wire this through FastAPI lifespan or an explicit injected
   runtime; do not create a hidden global store.
2. Verify `RESEARCHOS_MEMORY_ENABLED` off means no memory lookup and preserves
   existing behavior. Verify enabling requires a configured/injected store and
   makes the selected provider observable.
3. Verify `LAYA_CONTEXT_SELECTION` selects the Laya adapter only when memory is
   enabled; otherwise no unused selector/model should be created.
4. Test both workflows with an owned event loop and worker-thread execution,
   including retrieval timeout, store exception, empty results and shutdown.
5. Verify SSE and persisted run results include selector, context IDs, memory
   stage outcome, latency/token usage, and a visible degraded alert when lookup
   fails.
6. Test project isolation with two projects containing deliberately distinctive
   memory records; prove one project cannot retrieve the other’s record.

**Acceptance:** no cross-project leakage; selected context never exceeds the
memory budget; disabled mode is behavior-compatible; optional failures are
visible and do not masquerade as retrieved context; app startup/shutdown closes
owned resources.

**Progress:** complete. The lifespan-owned runtime is covered by tests with a
fake runtime. Memory-lookup failures set `degraded` and the `memory` alert
survives synthesize and verify. The SSE `done` event now includes
`memory_selector`, `memory_context_ids` and `memory_context`.
`tests/test_memory_failure_modes.py` covers failure, timeout, empty, isolation and
budget on both engines, plus an `/api/stream` two-project leakage test. Full suite
with `PYTHONPATH=src`: 450 passed, 9 skipped, 6 xfailed. A live PostgreSQL
runtime is still unexercised (P3).

### P2 — Measure Laya against greedy before any default change

**Goal:** Establish evidence for selector quality, not just code-path coverage.

1. Create a versioned, deterministic evaluation set with at least 30
   query/task/context cases and expert relevance labels. Include stale,
   superseded, continuity, irrelevant, and boundary-budget examples.
2. Run greedy and Laya on identical candidates, project scopes, and token
   budgets. Record candidate count, selected IDs, relevance labels, recall,
   precision/nDCG, token use, latency, fallback/error rate, and selector name.
3. Evaluate the real Laya package separately from `HeuristicLayaRouter`; never
   combine the two result sets or report heuristic behavior as model accuracy.
4. Pre-register the primary metric and non-inferiority guardrails before
   looking at the comparison. Keep greedy as default unless Laya improves the
   chosen metric without violating evidence, budget, or latency guardrails.
5. Add a representative end-to-end research run test asserting citation
   resolution and that memory-only IDs never appear as source citations.

**Acceptance:** reproducible report and dataset version checked in; all hard
invariants pass; rollout decision is based on recorded measurements. Numeric
promotion thresholds must be agreed before the run rather than invented after
seeing the result.

### P3 — Release rehearsal and operational readiness

**Goal:** Prove the supported deployment path without risking source data.

1. Run migrations and backup/restore rehearsal against a separate staging
   database, following a present rollback runbook.
2. Exercise model disabled, model provider failure, search partial failure,
   MCP-to-HTTP fallback, memory unavailable, cancellation and final persistence
   failure.
3. Confirm run/SSE status semantics, logs, retention, secrets configuration and
   workspace backup location. Record actual measured latency and resource use.
4. Demonstrate the complete offline path from seeded corpus through streamed
   answer, citation trace, saved run, and UI display.

**Acceptance:** recorded staging restore; no destructive test against source
data; each simulated failure is visible and has a bounded recovery behavior.

### P4 — One narrow acquisition-to-evidence vertical slice

Only after P0–P3: choose one provider (arXiv is already integrated), one
document type (PDF already supported), and one extraction output (source-linked
claims). Implement:

1. Stable source identity/version plus retrieval timestamp and provenance.
2. PDF text extraction and structured claim proposal with page/section
   references.
3. Schema validation and deterministic persistence; model output is not
   promoted directly to established fact.
4. Claim-to-source citations, contradiction/supersession links, and reviewable
   extraction status.
5. Evaluation for retrieval relevance, claim extraction precision/recall,
   provenance coverage, and citation correctness.

Do not begin P5 onward until this slice is trustworthy.

### P5 — Full ingestion and source registry

**Goal:** Understand documents, not just find them.

1. Source registry: stable identity, version, retrieval timestamp, license note
   and content hash per source, scoped by project.
2. Add GROBID or Docling behind an `Ingestor` interface for sections, reference
   lists, tables and figures; keep PyMuPDF as the fallback. Add Tesseract OCR for
   scanned PDFs.
3. Crossref (and DOI) metadata resolution alongside arXiv, Semantic Scholar and
   OpenAlex. Reconcile duplicates across providers.
4. HTML, Markdown, DOCX, CSV/XLSX and JSON inputs enter through the same
   interface; tabular inputs are registered for P7, not parsed into claims.
5. Untrusted-input handling: size/page limits, parser timeouts and isolation for
   parser failures.

**Exit gate:** a paper is parsed into sections, references, tables and metadata
with page references; parser failure is a visible `partial`/`error` and never an
empty success; extraction quality is measured on a labeled sample.

**Progress (partial):** `sources.py` keeps a per-project registry derived from
the graph: stable identity (DOI > arXiv > URL > node), content hash and version
history, access level (`metadata_only`/`abstract`/`full_text`), license note and
a citation key that never changes once assigned. `ocr.py` stores uploaded PDFs
immutably (sha256) and extracts page-located text via the PyMuPDF text layer,
Tesseract (through PyMuPDF) or the Nougat CLI; blank/scanned pages make the
result `partial`, a missing engine is `unavailable`. CSV/TSV/JSON/XLSX/Parquet go
to the dataset registry (P7). Not done: GROBID/Docling sections, references and
tables; Crossref/DOI resolution and cross-provider deduplication; HTML/DOCX
ingestion; a labeled extraction-quality sample.

### P6 — Artifact and LaTeX engine

**Goal:** Turn workspace evidence into publication-ready documents.

1. Document spec schema: title, sections, claim/source IDs, tables, figures,
   equations, bibliography style. Validate before building.
2. Bibliography generation from source registry records to `.bib`
   (BibTeX/BibLaTeX); citation keys are stable across rebuilds.
3. Jinja2 LaTeX templates with strict escaping of all untrusted text (papers, web
   text, model output). Initial templates: literature review, paper summary,
   comparative report.
4. Tectonic compilation in an isolated, time-limited subprocess: shell-escape
   off, no network after package fetch, bounded output size, captured log.
   Compilation errors surface as `error` with the log excerpt.
5. Pandoc conversion for DOCX/HTML; openpyxl for XLSX tables; Markdown report
   as the simplest output.
6. System 2 writes section prose only, from packed evidence; it does not emit
   LaTeX commands or the build configuration. Every cited ID must resolve or the
   build fails.
7. Artifact records link to source IDs, template version, spec and model/selector
   names, so a report can be regenerated.
8. API and UI: create-report, build-status and download endpoints; artifact list
   in the workspace view.

**Exit gate:** a literature review compiles to PDF from workspace evidence with
all citations resolved; an unresolved citation fails the build; hostile input
(LaTeX injection strings in titles/claims) is escaped; rebuilding from the same
spec is reproducible.

**Progress:** implemented in `artifacts.py`. `DocumentSpec` (paragraph, list,
claim, table, analysis summary, figure, equation blocks) is validated before
rendering; `[@id]` markers and `citations` must resolve to a project source or a
claim extracted from one, otherwise the build is `error` and no output is
written. A Jinja2 template with custom delimiters auto-escapes every value unless
explicitly marked safe; equations pass a command allowlist. Markdown, self-
contained HTML (CSP, served as attachment), LaTeX and BibTeX are always
produced; PDF runs Tectonic (`--untrusted`) or pdflatex (`-no-shell-escape`,
`openout_any=p`) in a deadline-bounded subprocess with secrets removed from the
environment. Build records hold spec hash, template/engine versions, generator
names and every source/claim/analysis/chart/dataset version used.
`compose_report_spec` builds a literature review/technical report deterministically
from workspace evidence; optional System 2 prose is kept only if every `[@id]` it
cites is in the supplied evidence. Tests cover escaping, unresolved citations,
reproducible `.tex`, and missing/failed/successful compilers (stubbed). A real
Tectonic 0.17.0 build of a composed review (hostile title/claim text, table,
figure, equation, bibliography) produced a 2-page PDF locally on 27 Nov 2026; the
first run needs network access to fetch the TeX bundle. Not done: Pandoc DOCX,
XLSX table export, and a Tectonic build in CI.

### P7 — Analytics and visualization

**Goal:** Real computation on real datasets, reproducibly.

1. Dataset registry with versions (Parquet storage), schema capture, and
   provenance (uploaded file hash).
2. Data management: cleaning, missing values, filtering, aggregation, joins and
   DuckDB SQL via a restricted query path.
3. Fixed operation catalog: descriptive stats, correlation, hypothesis tests,
   regression, confidence intervals, ANOVA, basic time series. Each operation
   has a typed parameter schema and records parameters plus dataset version.
4. Model request path: System 2 or Laya proposes `{operation, dataset, params}`;
   deterministic code validates and executes. No model-written code execution.
5. `create_chart(spec)` for line, bar, heatmap, scatter and distribution plots
   with Matplotlib (static) and Plotly (interactive); output includes image,
   metadata and the data used.
6. Integrate results into P6: tables and figures referenced from the document
   spec carry dataset version and analysis ID.

**Exit gate:** a user uploads a dataset and obtains a reproducible analysis and
chart; re-running with the same dataset version and parameters gives identical
numbers; numeric correctness is tested against reference values.

**Progress:** implemented in `analytics.py` and `charts.py`. Uploads are size-
and format-checked, hashed, parsed with deterministic type inference (or explicit
`column_types`), profiled, and stored as immutable versions; identical uploads
reuse the version. Nine validated transforms (select, rename, filter, drop/fill
missing, sort, deduplicate, cast, join) create derived versions with recorded
lineage. The catalog has 13 operations (describe, missing summary, value counts,
group aggregate, correlation, CI, t-tests, Mann-Whitney, chi-square, Shapiro-Wilk,
OLS regression, one-way ANOVA, time-series summary), each with a strict pydantic
parameter schema; analysis ids hash dataset version + operation + parameters, and
results record warnings and numpy/scipy versions. Tests check textbook reference
values and identical re-runs. Charts (line, bar, scatter, histogram, box,
correlation heatmap) render with matplotlib to PNG and deterministic SVG and
store the exact plotted data. Not done: DuckDB SQL query path, Parquet storage
(tables are stored as typed JSON), Plotly interactive output.

### P8 — Persistent research workspaces

**Goal:** Multiple independent projects as the central user-facing abstraction.

1. Workspace model: objectives, milestones, source library, datasets, claims,
   artifacts and decision history in one project record.
2. Artifact versioning and regeneration: when a source, claim or dataset
   changes, mark dependent reports and charts stale and offer a rebuild.
3. Explicit, permissioned cross-workspace retrieval; default is isolation.
4. Experiment history linking datasets, analyses, charts and reports.
5. End-to-end research workflow: objective -> plan -> discovery -> ingestion ->
   memory -> synthesis -> analysis -> visualization -> report, with each stage
   resumable from its recorded outputs where designed and tested.
6. Workspace-level backup/restore and export.

**Exit gate:** two independent projects run end to end without context or
provenance mixing; changing a source marks the right artifacts stale; a stale
report rebuilds from current evidence.

**Progress (partial):** all engine data is stored under
`<data_dir>/projects/<project_id>/` and every lookup is project-scoped (tested
for datasets over HTTP). Artifacts are versioned per build; `staleness` reports
changed/removed sources and claims, newer dataset versions and missing
analyses/charts, and `build(refresh_inputs=True)` re-runs the same analyses and
charts on the latest dataset versions before rebuilding. A bounded Lean adapter
(`proof.py`) screens out code-executing commands and reports
`verified`/`failed`/`rejected`/`unavailable` with axioms and diagnostics. Not
done: objectives/milestones/decision history in the project record,
cross-workspace retrieval policy, workspace-level export, and an end-to-end
objective -> report workflow in the research graph.

## 6. Cross-cutting acceptance criteria

These invariants apply to every phase:

- **Scope:** project IDs are enforced at retrieval and persistence boundaries.
- **Evidence:** every citation resolves to an available source/evidence record;
  background memory is never represented as a citation.
- **Budget:** selected memory tokens are at or below the requested allocation;
  total run budget allocation is explicit and non-negative.
- **Status:** `empty`, `partial`, `error`, `degraded`, and `failed` are distinct
  where applicable and reach API/SSE/run history.
- **Fallback:** optional model, memory, or MCP failures have a tested, bounded
  behavior; no success-shaped silent fallback.
- **Compatibility:** sequential and LangGraph engines preserve the same result
  contract and stage semantics.
- **Reproducibility:** record engine, selector, provider outcome, input IDs,
  token budget, timestamps and evaluation dataset version.
- **Persistence:** backup and restore are demonstrated in staging before
  claiming operational readiness.
- **Claims:** extracted claims carry origin and review status; none is shown as
  established fact without it.
- **Artifacts:** every generated document links to its source IDs, dataset
  versions, template version and spec, and fails to build on unresolved
  citations.
- **Computation:** numbers in reports come from the analytics engine, never from
  model text.
- **Untrusted input:** parsers, LaTeX compilation and analysis run with size,
  time and isolation limits.

## 7. Explicit non-goals (all phases)

- Replacing all memory storage or the existing workspace persistence.
- Durable pause/resume across process restart unless the product requires it
  and a persistent checkpointer is separately designed and tested.
- Promoting Laya to the default before labeled comparative evaluation.
- Adding Redis, a dedicated graph database, microservices, Kubernetes, or a
  distributed job system without measured need.
- Delivering several phases at once; P0-P8 ship in order with their gates.
- Model-written or arbitrary code execution in analytics.
- Separate microservices for the engines; they remain modules in one backend
  unless measured load requires otherwise.
- Claiming production readiness from unit tests, mocked model calls, a local
  demo, or an unverified restore statement.

## 8. Source of truth and maintenance

Use this plan for sequencing and verified system boundaries. Use
[`read.md`](read.md) for product direction, and accepted ADRs for binding
architecture decisions. Update this document when a code-level
acceptance gate passes; do not mark a phase complete based only on planned work
or a feature flag existing.

Relevant implementation and verification sources:

- [`src/researchos/orchestration/workflow.py`](../src/researchos/orchestration/workflow.py)
- [`src/researchos/orchestration/state.py`](../src/researchos/orchestration/state.py)
- [`src/researchos/memory/context.py`](../src/researchos/memory/context.py)
- [`src/ctxflow/laya_provider.py`](../src/ctxflow/laya_provider.py)
- [`src/ctxflow/laya_selector.py`](../src/ctxflow/laya_selector.py)
- [`src/researchos/api/server.py`](../src/researchos/api/server.py)
- [`tests/test_langgraph_workflow.py`](../tests/test_langgraph_workflow.py)
- [`tests/test_context_provider_wiring.py`](../tests/test_context_provider_wiring.py)
- [`tests/test_laya_context.py`](../tests/test_laya_context.py)
- [`docs/adr/001-orchestration-runtime.md`](adr/001-orchestration-runtime.md)
