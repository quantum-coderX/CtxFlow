# ResearchOS: Research Workstation Intent and Requirements

**Document type:** Product intent and capability requirements  
**Status:** Target vision; not a claim that every capability is implemented  
**Baseline checked:** 27 November 2026  
**Related implementation plan:** [Executionplan.md](./Executionplan.md)  
**Product vision source:** [read.md](./read.md)

## 1. The intent

ResearchOS should become an AI research workstation: a project-centered system
that helps a researcher move from a question and raw evidence to checked
analysis and reproducible research artifacts.

The goal is not to make another chatbot with a PDF upload button. ResearchOS
should connect the parts of research work that are usually scattered across
search engines, reference managers, notebooks, analysis scripts, proof
assistants, plotting tools, and document editors. The connection must be
traceable: a reader should be able to follow a statement in a report back to
the paper, page, dataset version, calculation, or formal check that supports
it.

The intended end-to-end loop is:

> Define an objective → acquire sources and datasets → understand and organize
> them → plan bounded work → run analyses and checks → synthesize findings →
> create figures and documents → review, reproduce, and revise the result.

ResearchOS should help with research, not take ownership of it. Researchers
remain responsible for choosing the question, approving consequential
decisions, interpreting results, and deciding what is ready to publish.

## 2. What a researcher should be able to do

Within one project workspace, a researcher should be able to:

1. State a research objective, constraints, and expected deliverables.
2. Search academic and other approved sources, including arXiv.
3. Add papers, notes, images, and structured datasets to a versioned project
   library.
4. Extract document structure, references, tables, figures, equations, and
   candidate claims with source locations.
5. Ask questions over the collected evidence and inspect citations and
   provenance.
6. Ask for a named statistical operation on a selected dataset version.
7. Generate visualizations from explicit chart specifications.
8. Request formal checking of supported mathematical statements or Lean
   projects through a dedicated proof-checking adapter.
9. Draft literature reviews, technical reports, and academic manuscripts from
   structured evidence and document specifications.
10. Re-run work after an input, dataset, method, or source changes and see which
    results and artifacts may need updating.
11. Inspect what ran, in what order, with which inputs, tool versions,
    parameters, outputs, warnings, and failures.

The workstation should support these activities as one coherent workflow,
while keeping domain engines independently replaceable. A failure in OCR, a
model provider, an MCP server, or a compiler must be visible and must not be
misreported as an empty successful result.

## 3. Core product principles

### 3.1 Evidence before confidence

ResearchOS should prefer a qualified, traceable answer over a confident but
unsupported one. It should distinguish:

- a source's reported statement;
- an extracted or model-proposed claim;
- a computation performed by a deterministic analysis engine;
- a formally checked proof result;
- a ResearchOS synthesis or interpretation;
- an unresolved question or disagreement.

These are different kinds of evidence and must not be presented as
interchangeable.

### 3.2 Reproducibility by construction

Each derived output should record enough information to reproduce or audit it:

- project and run identifiers;
- source or dataset identity, version, and content hash;
- extraction method and version;
- analysis operation and typed parameters;
- model, selector, prompt/template, and tool versions where applicable;
- citations, page/section/span locations, warnings, and result status.

Live provider calls and generative text may not be byte-for-byte reproducible.
ResearchOS should record that limitation and preserve the inputs and reported
provider outcomes needed for a meaningful replay or audit.

### 3.3 Models propose; validated engines execute

System 2 models may interpret a request, propose a plan, draft prose, or request
a named operation. Laya may make supported structured decisions. Deterministic
ResearchOS code must validate identifiers, schemas, permissions, budgets,
dependencies, and citations before execution or persistence.

Models must not supply arbitrary Python, shell commands, SQL with unrestricted
access, LaTeX build commands, or filesystem operations for execution.

### 3.4 Project isolation and researcher control

Project data, sources, datasets, memory, and artifacts should be isolated by
default. Cross-project use requires an explicit policy and user intent. Users
should be able to inspect, correct, replace, or remove project-level inputs and
derived artifacts according to workspace policy.

### 3.5 Modularity without premature services

ResearchOS should remain a modular application unless operational evidence
requires a different deployment shape. Domain engines should have clear
interfaces and may use local libraries, subprocesses, or MCP adapters behind
those interfaces. MCP is an interoperability mechanism, not the complete
application architecture.

## 4. Target architecture and division of responsibility

The intended system is a set of cooperating layers:

```text
Researcher
  |
  v
Project workspace and user-facing application
  |
  v
Validated planning and orchestration
  |-- bounded task DAG, budgets, cancellation, progress, failure handling
  |-- allowlisted, typed tool registry
  |
  +-- System 2: question understanding, research synthesis, prose
  +-- Laya: constrained structured decisions, when enabled and evaluated
  +-- CtxFlow: project-scoped memory, provenance, relations, continuity
  +-- Domain engines:
        acquisition and ingestion
        dataset and analysis engine
        visualization engine
        formal proof-checking adapter
        artifact and document engine
  |
  v
Versioned, auditable sources, datasets, analyses, checks, and artifacts
```

### 4.1 CtxFlow and ResearchOS

CtxFlow is the memory and context substrate. It should retain useful,
project-scoped knowledge, relationships, and provenance. It is not the
authority for raw datasets, statistical calculations, proof results, or
generated files. Those belong to their corresponding versioned domain
artifacts, with links back to relevant evidence and memory.

The current Rust memory core is treated as working for this next-layer effort
based on the project owner's VM validation. This document does not claim an
independent re-validation of that core.

### 4.2 System 2

System 2 is the generative reasoning layer. It should:

- interpret research questions and user constraints;
- propose structured research plans;
- synthesize supplied and retrieved evidence;
- draft section prose and explain results;
- identify assumptions, limitations, and gaps;
- request fixed, typed tool operations.

System 2 should receive selected evidence and explicit tool results rather
than unrestricted access to the entire workspace. Generated prose is not
itself verification.

### 4.3 Laya: judge, coordinator, and router - with bounded authority

The product intent is to use Laya more broadly than memory selection, but only
where its structured decision behavior is appropriate. The desired roles are:

**Judge of structured candidates**

- Score or classify candidate memories, sources, evidence items, analysis
  routes, or draft claims against an explicit rubric.
- Return a bounded decision, confidence or score, and abstention/uncertainty
  where supported.
- Help prioritize what deserves human or System 2 attention.
- Never replace citation resolution, numeric recomputation, source validation,
  or a formal proof checker.

**Coordinator signal**

- Help prioritize among a fixed set of eligible subtasks or recommend which
  work should happen next.
- Provide structured decisions to the orchestrator; do not own execution,
  retries, concurrency, persistence, or permissions.
- Leave scheduling and state transitions to deterministic orchestration code.

**Router among approved capabilities**

- Select or rank a route from a finite, pre-approved set of task types and tool
  categories.
- Never invent a tool, alter its schema, grant a permission, or bypass a
  deterministic allowlist.
- Have the proposed route validated before any tool is called.

These roles require task-specific interfaces, labeled evaluation data,
calibration checks, abstention behavior, and measured comparison with
deterministic baselines. Laya should not be treated as a general conversational
planner or an oracle. In the current repository, Laya is an opt-in
memory-context decision adapter; its broader coordinator/router/judge roles
are future requirements, not verified current capabilities.

## 5. Capability requirements

### 5.1 Research acquisition

Researchers need discovery across academic and approved web sources. The
system should support provider adapters, normalized metadata, deduplication,
version awareness, bounded requests, and explicit outcomes such as `ok`,
`empty`, `partial`, `error`, or `disabled`.

The arXiv MCP integration is a current foundation. Other providers and MCP
servers should be added behind typed adapters with timeouts, health/error
reporting, and provider-specific provenance.

Finding a paper is not the same as acquiring its full text. ResearchOS must
show whether a result is metadata-only, abstract-only, full-text, unavailable,
or failed.

### 5.2 Dataset ingestion and understanding

Datasets should be first-class, versioned workspace resources, not temporary
files passed invisibly to a model.

The ingestion path should:

- accept supported tabular formats such as CSV, Excel, and Parquet;
- validate file size and format before parsing;
- calculate and store a content hash;
- record the source, upload time, project, parser, and dataset version;
- capture column names, data types, row counts, missingness, and basic
  quality warnings;
- make transformations explicit and record their parameters;
- preserve the original input and avoid silently overwriting a dataset version.

Understanding a dataset should provide a factual profile and schema. An LLM
may describe that profile or suggest follow-up questions, but it must not
silently infer data types, change values, or decide that a data-quality issue
does not matter.

### 5.3 Analysis and statistics

Analysis should run through a fixed, typed operation catalog rather than
model-written code. The eventual catalog may include:

- descriptive statistics and distributions;
- missing-value summaries and explicitly requested cleaning;
- filtering, grouping, aggregation, and validated joins;
- correlation and association measures;
- confidence intervals and supported hypothesis tests;
- regression and ANOVA;
- basic time-series summaries.

Each request should identify a dataset version, named operation, typed
parameters, and expected output shape. The engine should reject invalid
parameters, return warnings where assumptions are not met, and record the
actual parameters and computation environment.

NumPy and other numerical libraries may be used inside these reviewed,
deterministic operations. They are implementation dependencies, not a reason
to enable arbitrary Python execution.

### 5.4 Visualization

Visualization should be driven by a typed chart specification, dataset
version, and selected columns. Initial chart families may include line, bar,
scatter, distribution, and heatmap charts.

Charts should:

- reference the exact dataset version and analysis result they visualize;
- validate axes, columns, aggregation, labels, and chart-specific parameters;
- avoid misleading defaults or hidden transformations;
- preserve the chart specification and the data/result identifiers;
- be reproducible when regenerated from the same versioned inputs.

Matplotlib is a candidate for static figures; Plotly may be used where
interactive charts add value. The chart engine, not the model, should render
the figure.

### 5.5 OCR and scientific document understanding

ResearchOS should understand machine-readable and scanned academic documents,
including their structure and source locations. Parsing may combine PyMuPDF,
GROBID or Docling, OCR, and targeted table/figure extraction behind an
ingestion interface.

Meta's Nougat is a candidate for scientific-paper OCR and mathematical
content. It should be evaluated on representative papers for text accuracy,
equation fidelity, tables, runtime, hardware needs, and failure handling
before being selected as a default. OCR output must retain page or region
locations and must be treated as extracted evidence, not ground truth.
Tesseract or another OCR adapter may remain useful for scanned-document
coverage or as a fallback.

### 5.6 Formal proof checking

Formal proof checking is distinct from ordinary claim criticism. A future
proof-checking capability should connect to Lean through a dedicated
Lean/LSP/MCP adapter where appropriate.

The adapter should return structured information such as:

- the theorem or declaration checked;
- the Lean/project/toolchain version;
- whether the file or target elaborated successfully;
- diagnostics and source locations;
- dependencies and relevant build context;
- a clear distinction between a checked proof, a failed check, and an
  unavailable checker.

Natural-language reasoning, an LLM's confidence, and a citation critic must
never be represented as a Lean-verified proof. The checker should operate in
a controlled project directory with bounded resource use.

### 5.7 Literature reviews, technical reports, and manuscripts

Researchers should be able to create:

- literature reviews and paper comparisons;
- technical reports with methods, results, limitations, and appendices;
- academic manuscript drafts with sections, equations, figures, tables, and
  bibliography;
- concise project notes and reproducible Markdown summaries.

The model should generate or edit section prose from a validated document
specification and selected evidence. It should not emit arbitrary LaTeX
commands or control the compiler configuration.

The document engine should:

- validate the document specification before rendering;
- use versioned templates and stable citation keys;
- resolve every cited source or dataset identifier before build;
- escape untrusted source text and model output;
- generate bibliography data from the project source registry;
- compile LaTeX/PDF in an isolated, time-limited environment with
  shell-escape disabled;
- surface compiler errors and logs instead of returning a success-shaped
  empty artifact;
- record the specification, template, evidence references, and build result
  so a document can be regenerated.

Tectonic is a candidate for LaTeX/PDF compilation; Pandoc may support
DOCX/HTML conversion. Toolchain choice should remain behind the artifact
engine interface.

## 6. Harness and parallel execution requirements

The harness is the controlled bridge between proposed work and real
capabilities. It should support:

- a registry of named tools with descriptions and input/output schemas;
- allowlisted operations selected by deterministic policy;
- validated task plans represented as a dependency graph;
- bounded concurrency, task count, time, memory, and output size;
- declared references between task outputs and later inputs;
- progress, cancellation, and per-task timing;
- dependency-aware failure behavior;
- run-level trace and reproducibility metadata.

Independent tasks should execute concurrently when resource limits permit.
Dependent work should wait for its inputs. If a prerequisite fails, its
dependents should be marked blocked; unrelated work may continue. Partial
success must be reported as partial, never silently promoted to full success.

Examples of parallel work include searching multiple approved providers,
processing independent papers, or running separate pre-approved analyses on
the same dataset version. A model may propose the plan, but the harness must
validate its tools, schemas, dependencies, and budgets before running it.

The current Python harness provides a bounded registered-skill DAG and
explicit output references. It is an internal execution seam; it is not yet a
complete dataset, OCR, proof, or document workflow and is not exposed as an
unauthenticated arbitrary-plan API.

## 7. Evidence, artifacts, and project history

Every important output should be a project artifact with an identity and
relationships to its inputs. Expected artifact types include:

- source records and parsed document versions;
- datasets and transformed dataset versions;
- analysis results and charts;
- proof-check runs and diagnostics;
- report specifications, generated source files, PDFs, and exports;
- workflow plans, tool results, warnings, and human review decisions.

The workspace should make relationships navigable. A chart should lead to its
analysis and dataset. A report claim should lead to its cited source or
analysis. A proof result should lead to the checked Lean target and toolchain.
When an input changes, dependent outputs should be marked stale or offered for
regeneration rather than silently treated as current.

## 8. Safety, privacy, and trust boundaries

The system must:

- treat papers, websites, datasets, OCR results, and MCP responses as
  untrusted data;
- defend against prompt injection by preventing retrieved content from
  granting tool authority;
- prohibit arbitrary shell, filesystem, and code execution by default;
- restrict each operation to its validated inputs and project scope;
- isolate parsers, OCR, proof checking, and document compilation as required;
- enforce resource limits and report timeouts and failures explicitly;
- avoid exposing secrets in prompts, traces, or generated artifacts;
- require explicit policy for external network access and cross-project data;
- distinguish provenance-backed evidence from model inference and memory.

An allowlist entry alone does not make a tool available or safe. A capability
is usable only when its implementation, schema, execution policy, isolation,
and tests are in place.

## 9. Current baseline and gaps

This is a product-intent document, so the status below is a snapshot rather
than a promise of permanent completeness.

### Present foundations

- CtxFlow memory and graph infrastructure, including the Rust core the project
  owner reports validating in a VM.
- ResearchOS research workflow, typed skill registry, evidence/citation checks,
  and existing project/run concepts.
- Literature acquisition, arXiv MCP with fallback, web search, source
  fetching, and PDF reading.
- A literature-review skill, with current behavior and quality bounded by its
  implementation and model configuration.
- Bounded parallel research retrieval.
- Opt-in Laya context selection.
- An internal bounded tool-plan harness over registered skills, including the
  workstation engines below.
- Versioned dataset registry (CSV/TSV/JSON, XLSX/Parquet with optional
  libraries), factual profiling, validated transforms with lineage, and a fixed
  13-operation statistics catalog with reproducible, versioned results.
- Chart rendering from typed chart specs (matplotlib PNG/SVG) recording the
  dataset version and plotted data.
- Project source registry with stable citation keys and BibTeX; validated
  document specifications rendered to Markdown, HTML and LaTeX with escaping;
  isolated Tectonic/pdflatex compilation; artifact versions, staleness and
  refresh-rebuild.
- Stored PDFs with page-located text extraction and optional Tesseract/Nougat
  OCR adapters.
- A bounded Lean 4 proof-checking adapter (requires a local Lean toolchain).

### Not yet implemented as workstation capabilities

- GROBID/Docling structure extraction, Crossref resolution, DuckDB SQL.
- Measured Nougat quality; a Lean/LSP MCP server integration and tests against
  a real Lean toolchain.
- Pandoc DOCX/XLSX exports.
- End-to-end user workflow connecting all these engines from one research
  objective, plus workspace objectives, milestones and decision history.
- Measured Laya task routing, coordination, and judgment beyond its current
  memory-selection scope.

## 10. Suggested delivery sequence

The sequence should establish trusted inputs and artifacts before combining
them into the full user experience:

1. **Foundation and contracts:** keep the existing research workflow,
   workspace, provenance model, tool registry, and bounded harness stable.
2. **Ingestion and source registry:** add versioned source and dataset records,
   parsing/OCR adapters, schema capture, and evidence locations.
3. **Document artifacts:** build validated document specifications,
   bibliography generation, safe templates, and isolated LaTeX/PDF output.
   This enables literature reviews before statistical analytics are complete.
4. **Analytics and visualization:** add a fixed, tested operation catalog,
   reproducible dataset-versioned results, and chart specifications.
5. **Formal proof adapter:** connect Lean checking behind a typed interface,
   preserving diagnostics and toolchain identity.
6. **Integrated research workflows:** combine acquisition, ingestion, memory,
   parallel analysis, proof checks, synthesis, and artifact generation with
   traceable dependencies and stale-output handling.
7. **Laya expansion:** only after collecting labeled evaluations, expand
   Laya's structured decisions to routing, prioritization, or evidence
   assessment. Keep deterministic policy and validators authoritative.

Each stage should have focused correctness, security, and reproducibility
tests. A capability is ready to advertise only after its acceptance criteria
are met on representative data, not merely because its dependency or MCP
server can be installed.

## 11. Definition of the intended product

ResearchOS will feel like a research workstation when a researcher can start
with a question, build a trustworthy project corpus, inspect and analyze a
versioned dataset, formally check supported claims where appropriate, and
produce a cited, reproducible report or manuscript without losing the trail
from result back to evidence.

The key product property is not the number of integrated tools. It is that
the tools cooperate through validated contracts, preserve provenance, expose
uncertainty and failure, and leave the researcher able to understand and
reproduce what happened.
