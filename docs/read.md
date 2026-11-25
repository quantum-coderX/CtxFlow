
### ResearchOS — Vision, Architecture & Technical Blueprint 

My vision for ResearchOS is to build an intelligent, persistent research workstation that can independently assist with the entire research lifecycle — from discovering knowledge to analysing evidence, managing context, conducting experiments and producing publication-ready outputs.

I don't want ResearchOS to become another chatbot with a PDF uploader, a vector database and a few tools attached.

I want it to be a system in which specialised components work together, each with a clearly defined responsibility.

The central architectural idea is:

System 1 decides. System 2 reasons. CtxFlow remembers. ResearchOS coordinates. Tools execute.

And importantly, none of these components should need to pretend to be a general-purpose intelligence.

### 1\. The vision

![TarCite — Research Toolkit & Citation Assistant. Private by Design, Run Locally.](https://images.openai.com/static-rsc-4/lhpZxfNJ_cdEyRyu3tfyDdwquiWr4_PFzg6RSqfp5P4DpuiwGnxTQ7HrgKekmAzAg9MPwzpUozjIl-jYaB4OIDb33HJzMQ5uWLzmQfPb_-bdjaN7DKlw69xsyNAhpiSSHDoOOulU8hLklrVG5rlpaugOYkqy_k9rDUOcTspRB8MHswQt2bUabb9ohPpA4cS0?purpose=fullsize)

### ResearchOS

### An intelligent operating environment for research.

A persistent environment where research projects, knowledge, evidence, data, experiments and generated artifacts are connected rather than scattered across different applications.

Discover

Search the web, academic literature and external knowledge sources.

Understand

Reason over documents, evidence, claims and relationships.

Remember

Maintain persistent, evolving context through CtxFlow.

Analyse

Process datasets, run statistical analysis and experiments.

Visualise

Generate graphs, diagrams, tables and analytical views.

Publish

Produce structured reports, LaTeX documents and PDFs.

### The fundamental difference

A conventional AI research assistant generally works like this:

User asks → LLM retrieves information → LLM generates an answer.

ResearchOS should work more like this:

User defines a research objective → ResearchOS discovers information → evidence is extracted → Laya evaluates structured decisions → CtxFlow maintains knowledge → System 2 reasons over the relevant context → analytical tools perform computations → the document engine produces artifacts.

This is a much more substantial engineering objective.

### 2\. The complete architecture

I would organise ResearchOS into nine major subsystems, with an orchestration layer coordinating them.

### ResearchOS Core

User interface · Project management · API · Permissions

Orchestration and workflow control

Laya

System 1

Fast structured decisions, classifications, rankings and probabilities.

Reasoning Engine

System 2

LLM-based reasoning, research synthesis and complex problem solving.

CtxFlow

Memory

Persistent context, memory lifecycle, evidence and knowledge graphs.

Research Acquisition

Discovery

Web search, arXiv, scholarly APIs and document retrieval.

Ingestion & Extraction

Processing

PDF parsing, OCR, metadata, tables and claim extraction.

Analytics Engine

Computation

Data management, statistics and reproducible experiments.

Visualization Engine

Presentation

Charts, scientific plots, diagrams and interactive graphs.

Artifact Engine

Publishing

LaTeX, PDF, DOCX, XLSX, Markdown and HTML.

MCP & Integration Layer

External tools · APIs · Filesystem · Search · Academic services

Shared Infrastructure

PostgreSQL · pgvector · Redis · Object Storage · NetworkX · Audit Logs

These are logical boundaries, not nine mandatory microservices. For your current team and deployment constraints, I would implement most of them as modular Python packages inside one backend initially.

### 3\. The intelligence layer

This is arguably the most important part of ResearchOS.

### 3.1 Laya — System 1

Constrained decision intelligence

Laya is not a conversational LLM. It should remain a specialised decision engine based on its actual model capabilities.

Its responsibility is to make fast, structured decisions.

Examples:

| Task | Decision |
| --------------------- | --------------------------------------------------- |
| Memory classification | Which category? |
| Relevance | Is this relevant? |
| Candidate selection | Which is most suitable? |
| Deduplication | Are these duplicates? |
| Memory retention | Should this be retained? |
| Retrieval ranking | Which candidates rank highest? |
| Evidence evaluation | Which candidate best satisfies a defined criterion? |

An illustrative output:

```json
{
  "task": "memory_relevance",
  "decision": "relevant",
  "probabilities": {
    "relevant": 0.87,
    "partially_relevant": 0.09,
    "irrelevant": 0.04
  }
}
```

These values are illustrative, not guaranteed Laya output fields.

What I would not ask Laya to do:

* Write research reports.

* Generate arbitrary explanations.

* Summarise papers in natural language.

* Perform mathematical derivations.

* Conduct open-ended research.

Those are responsibilities for other components.

One critical engineering requirement: evaluate calibration, class imbalance and abstention. A model's probability output is not automatically a calibrated probability of correctness.

### 3.2 System 2 — Reasoning Engine

Deliberative intelligence

This is where your Groq and Ollama integration belongs.

System 2 handles:

* Understanding research questions.

* Synthesising multiple papers.

* Comparing methodologies.

* Explaining mathematical concepts.

* Identifying research gaps.

* Forming hypotheses.

* Generating structured document content.

* Planning complex workflows.

I would retain provider abstraction so you can switch between hosted and local models.

The reasoning engine should receive carefully selected context from CtxFlow rather than the entire memory database.

### 3.3 How System 1 and System 2 cooperate

Consider a question:

"Find the most relevant papers on memory management in LLM agents and compare their approaches."

* Research Acquisition finds 50 papers.

* Ingestion extracts their metadata and relevant content.

* Laya ranks candidates using the defined relevance criteria.

* System 2 examines the shortlisted papers.

* CtxFlow stores claims, evidence and relationships.

* The analytics and artifact engines prepare comparison tables and reports.

This gives you a division of labour instead of sending every task to an expensive generative model.

### 4\. CtxFlow — The memory and context infrastructure

Core infrastructure

I would treat CtxFlow as one of the defining technologies of ResearchOS.

Its job isn't simply to store embeddings or retrieve semantically similar documents.

Its job is to maintain an evolving representation of what the system knows, where that knowledge came from, how it relates to other information and whether it is still valid.

### Proposed memory structure

| Memory type | Example |
| --------------- | --------------------------------------------- |
| Episodic | A paper was analysed on Monday |
| Semantic | A particular algorithm has a defined property |
| Procedural | How an experiment was conducted |
| Working context | Information relevant to the current task |
| Observational | A pattern identified in a dataset |
| Relational | Paper A extends the method from Paper B |

### Important capabilities

* Claim-level memory rather than only document-level memory.

* Source and provenance tracking.

* Temporal validity.

* Duplicate detection.

* Memory consolidation.

* Context prioritisation.

* Retrieval based on multiple signals.

* Contradiction detection.

* Token-budget-aware context construction.

### Technology

| Component | Proposed technology |
| ------------------ | ------------------------------- |
| Persistent storage | PostgreSQL |
| Vector search | pgvector |
| Relationships | NetworkX initially |
| Short-lived state | Redis |
| Workflow state | LangGraph |
| Retrieval | Hybrid lexical + vector + graph |
| Decision layer | Laya |

I'd also make memory operations auditable. Laya should propose structured decisions; deterministic code should validate and execute writes.

My preferred design: CtxFlow owns memory. Laya operates the memory lifecycle. System 2 consumes and reasons over the memory.

### 5\. Research Acquisition & Discovery Layer

External knowledge infrastructure

ResearchOS must be able to discover information beyond its existing knowledge base.

I would build this as a provider-agnostic acquisition layer, with separate connectors for different source types.

### 5.1 Web search

Purpose: discover current information, technical documentation, research discussions and external resources.

Proposed integrations:

* DuckDuckGo — web search.

* Brave Search API — structured search API.

* Tavily — research-oriented web discovery.

* Custom crawlers — retrieve selected websites and documentation.

Don't integrate all of these immediately. Start with DuckDuckGo and one reliable API provider.

### 5.2 Academic research

![arXiv Archives - University Library TU Hamburg](https://images.openai.com/static-rsc-4/Es_2C7iz1tqspRpsya22PNmU7d4-Al3Ku2tEe__GSI2Dvq8qm04wGyk8am8cM06mtMBUEH_ITlkoliqYPcdqaVPbOBid9Ue9oVa2_-K-kD5wWVt30WihXfmx1LKBTR93z_u0-CZMzM1JzIgK8A6ZNmjKx398SuzWGXfRIrzNW3KIrGV22TQSENhuHROzlrJP?purpose=fullsize)

arXiv + arXiv MCP

Search preprints, retrieve metadata, access paper versions and download available PDFs.

[API documentation](https://info.arxiv.org/help/api/index.html?utm_source=chatgpt.com)

![Semantic Scholar | AI-Powered Scientific Research Tool](https://images.openai.com/static-rsc-4/XowINoWDFK3JCIXBEaAxJwLL3d80eXvE42B7MI3vXVPRNCJuKEgdzATdjolZLW3C6Wlj8ZtMf_D9Tf35wgSit2ooaSKnMr5t8eufL_V8FQ3M9ENUcoBQNtjPs-kOtE8xLU_25vVZYJX9S91Pk21GU_xZDDX7OII450AhZnHOfBCl9nUxDlNO5uSDtQ0tk69y?purpose=fullsize)

Semantic Scholar

Citation information, related papers, authors and scholarly discovery.

[API documentation](https://api.semanticscholar.org/?utm_source=chatgpt.com)

![Crossref Logo PNG Vector (AI) Free Download](https://images.openai.com/static-rsc-4/5uZSXLjeRNeFjLfPyV5XC6ni0Un7GnGgp2ekcXeBxZYpbh_6T5XbtvP1JRvlkeqdrkhHRXojtushOfvr8kczsELqDqMClkKYLSy_6qH0cv1fCR8dI1DPgF0C4oqGnpZcjCYpgyKgzRPQDwnbZfUqgkGneF8DmAO3ANoXiNOD6OPk6kOsPR6bcL9cRpvIjBOV?purpose=fullsize)

Crossref

DOI resolution, publication metadata and bibliographic records.

[Documentation](https://www.crossref.org/documentation/?utm_source=chatgpt.com)

![OpenAlex Archives - OpenAlex blog](https://images.openai.com/static-rsc-4/0CfxdkNRMiTeYED4tBhvcZVHhAhqbUkrBfhWNxSyiSvGFpBg7WmqhibXa-ykw-HrJPb50YYda7mVX7cs-rcH2D2uH5hHDdkmXxHpZ6ME3GQYhufm4zZemYObdWzg_b7UAgwThBTVc2wzfkgKEa_UukZHlX7fZt8K4ujMCPRb7aTfiAS-rPuy9lcWiOrYkqfJ?purpose=fullsize)

OpenAlex

Scholarly works, institutions, authors and research relationships.

[Documentation](https://docs.openalex.org/?utm_source=chatgpt.com)

### 5.3 MCP integration

I'd make each external capability accessible through MCP.

For example:

```text
mcp/
├── web_search/
│ ├── search_web
│ ├── fetch_page
│ └── crawl_page
│
├── arxiv/
│ ├── search_papers
│ ├── get_metadata
│ ├── download_paper
│ └── get_paper_versions
│
├── scholarly/
│ ├── search_citations
│ ├── get_author
│ └── find_related_papers
│
└── filesystem/
    ├── read_document
    └── list_project_files
```

MCP should be the interoperability layer, not the entire application architecture. Internal services can call ordinary Python APIs directly where that is simpler.

### The source-of-truth principle

Every piece of acquired knowledge should preserve:

* Original source.

* Source identifier.

* Retrieval timestamp.

* Document version.

* Extraction method.

* Relevant page or section.

* Claim-to-evidence mapping.

This is essential for reliable citations and reproducible research.

### 6\. Ingestion & Extraction Engine

Knowledge acquisition pipeline

Finding a paper is not the same as understanding its contents.

ResearchOS needs an independent document-processing pipeline.

### Supported inputs

* PDF research papers.

* DOCX documents.

* HTML articles.

* Markdown files.

* CSV and Excel datasets.

* Images and scanned documents.

* JSON and structured data.

### Proposed technology

| Function | Technology |
| ------------------------------- | ---------------------------- |
| PDF text and layout extraction | PyMuPDF |
| Complex academic PDF extraction | GROBID |
| Document parsing | Docling |
| OCR | Tesseract |
| Metadata | Crossref / arXiv |
| Structured extraction | System 2 + schema validation |

I'd particularly consider GROBID for academic papers because it can extract structured bibliographic information and document sections.

### Extraction pipeline

Source document

Document parser

Text · Tables · Figures · References

Structured extraction

Sections · Claims · Methods · Results

Validation and evidence linking

Source attribution · Schema checks · Confidence

CtxFlow

Persistent claims and research knowledge

One important detail: extracted claims must not become established facts automatically. Preserve whether something is an author-reported result, a system-generated inference or an independently verified observation.

### 7\. Analytics & Data Management Engine

Computational infrastructure

This is where ResearchOS becomes more than a literature assistant.

It should be capable of working with real datasets, performing reproducible analysis and producing usable outputs.

I would split this into three components.

### 7.1 Data management

Support:

* CSV and XLSX ingestion.

* JSON and Parquet.

* Data cleaning.

* Missing-value handling.

* Filtering and transformations.

* Aggregation.

* Joining datasets.

* SQL querying.

* Dataset versioning.

Recommended stack:

| Technology | Responsibility |
| ---------- | -------------------------------------- |
| Pandas | General data manipulation |
| Polars | Efficient larger-scale data processing |
| DuckDB | Analytical SQL |
| PostgreSQL | Persistent structured records |
| Parquet | Efficient dataset storage |

### 7.2 Statistical analysis

ResearchOS should eventually provide:

* Descriptive statistics.

* Correlation analysis.

* Hypothesis testing.

* Regression.

* Distribution analysis.

* Confidence intervals.

* ANOVA.

* Basic time-series analysis.

* Experiment comparisons.

Use SciPy, Statsmodels and scikit-learn where appropriate.

Crucially, the reasoning model should not calculate statistical results by improvisation. It should request actual computations from the analytics engine.

### 7.3 Visualization

![Long-Term Effects of the US Medical Research Effort During World War II | NBER](https://images.openai.com/static-rsc-4/aTKjZ2OHINouSMDOJRpyYvqmJrmecK5AR-L1z2bn7s0q5FkCyD8P_NORMPkTxRr-kDedi6kSmQ5C8hfMscLQbE44Pj6OtJNozguJJ4DTdVvQd7Bgfds8-_zynWyH-nvBtDlhEcr_sITNYqbrh9mBtr79GvLsU0Z_cHOF9jQlzpzBTLy9V2MnNvSD-Ym9n9kV?purpose=fullsize)

Line charts

Trends and time series

![Aging Exosomes PBS Control Behavioral Assay Grouped Bar Plot, Cell Metabolism 2025 | Plottie](https://images.openai.com/static-rsc-4/2IKcZRc11DVEA5IS4dt9f_Udj552IB9WflDZHK6Asmrthjkws6iVcBWMIfBesNoz_DHE7-seaScw485K6IWv3HXNaMUICVDv0177kplQkOs_jN_RC0Udkq05Iw2-ILm8a3mT61stEyFpwpUsauzuqWS0XU33jj5Oo0e4ARBq8NqB49vaBkljSB_yf9HRYv-f?purpose=fullsize)

Bar charts

Comparative results

![상관분석, 상관행렬(correlation matrix), heatmap](https://images.openai.com/static-rsc-4/cPZSm-TLxJT4FhxSG6vgXQ6TxqTNv-xsI4knr7ICN1ljIgHzMwBMxAoTbhvjJ0Vp6DLD1w4uDz5Wag5TPII2OAnsq41YUQ9MVG307pIcAGW3uFgb0M9LsWZNwGy7uxmaijZsbh89CWcsJnL-IAK7cDSfP7t4qi_zb19JKft-fe0mJB_B1gohmQMKupS6J49z?purpose=fullsize)

Heatmaps

Correlation and matrices

![Linear regression with statistics on top of a scatterplot](https://images.openai.com/static-rsc-4/Xm0VSoZowI09kV9L9pV1e8lwvakEzHKKzr4fJTh_oYqQB9u_ozmROxXQrDQvd5KUthVWKVdiG1ZaaTYW715MZOkKNBjVAj0O1WIdq-GyaxLYZdh8_4drg9f-Txi5pebkRCNypW8QTe1eJdZnJERJMLxmmZMp7tRZdUfFgLuQ5W2yYcMKO6ZPSkfGLUZiwnlG?purpose=fullsize)

Scatter plots

Relationships and distributions

Recommended stack:

* Matplotlib for publication-ready static graphs.

* Seaborn for statistical visualization.

* Plotly for interactive charts.

* Graphviz for architecture and dependency diagrams.

* NetworkX for graph analysis and knowledge visualization.

I'd have a unified visualization API that accepts structured datasets and chart specifications.

For example:

```python
create_chart(
    dataset="experiment_results",
    chart_type="line",
    x="epoch",
    y="accuracy",
    group_by="model",
    output="png"
)
```

The output should include the actual image, chart metadata and the data used to create it.

### 8\. Artifact & Document Engine

Major user-facing capability

This subsystem turns ResearchOS's work into deliverables.

It should support academic reports, research summaries, mathematical documents, technical reports and general-purpose exports.

### 8.1 Document generation pipeline

Document specification

Title · Structure · Sources · Tables · Figures · Equations

Content preparation

System 2-generated prose and validated structured data

LaTeX

Academic PDF

DOCX / HTML

Editable documents

Final artifacts

PDF · DOCX · XLSX · HTML · TEX · BibTeX

### 8.2 Recommended stack

| Technology | Use |
| ----------------- | ---------------------------------------------- |
| Pandoc | Document conversion |
| Tectonic | LaTeX compilation |
| Jinja2 | Document templates |
| BibTeX / BibLaTeX | Bibliography |
| Matplotlib | Embedded figures |
| python-docx | DOCX generation |
| openpyxl | XLSX generation |
| ReportLab | Direct PDF generation where LaTeX isn't needed |

I would use Pandoc + Tectonic as the primary academic publishing pipeline, but retain ReportLab for simpler programmatic PDFs.

### 8.3 Report types

* Research paper summaries.

* Literature reviews.

* Comparative research reports.

* Technical documentation.

* Experiment reports.

* Statistical analysis reports.

* Project documentation.

* Mathematical reports.

Every generated document should retain links to its source records and the versions of the data used.

This gives ResearchOS an important capability: it can regenerate a report when the underlying evidence or dataset changes.

### 9\. Persistent Research Workspaces

My strongest architectural recommendation

This is the part I would particularly emphasise.

ResearchOS should not be one giant conversation with a shared memory pool.

It should support persistent, independent research workspaces.

Imagine creating a project called:

Context Management for LLM Agents

Research Workspace

Active

### Context Management for LLM Agents

Illustrative workspace structure

Literature

### 48

Paper records

Knowledge

### 326

Illustrative claim nodes

Datasets

### 6

Dataset versions

Artifacts
### 12 

Reports and figures

Workspace navigation

Research conversations

Paper library

Context and knowledge graph

Experiments and analysis

Visualizations

Generated documents

Each workspace should have:

* Its own source library.

* Its own datasets and experiments.

* Its own project-specific context.

* Its own generated artifacts.

* A history of decisions and changes.

* Its own research objectives and milestones.

CtxFlow can maintain global knowledge alongside workspace-specific memory, but cross-workspace retrieval should be explicitly controlled.

This would allow a researcher to work on multiple independent topics without accidentally mixing their contexts.

### 10\. How everything works together: An end-to-end example

Let's take a realistic use case.

A user creates a research project:

"Investigate how token-budget-aware retrieval affects the performance of LLM agents."

### Research execution pipeline

* Research planning

    System 2 interprets the objective and proposes research questions, search criteria and an initial workflow.

* Discovery

    Web search and arXiv MCP discover relevant papers and technical resources.

* Document ingestion

    Papers are downloaded, parsed and converted into structured representations.

* System 1 evaluation

    Laya ranks papers, classifies claims and evaluates candidates according to defined criteria.

* Memory construction

    CtxFlow stores claims, sources, methodology, results and relationships.

* Reasoning

    System 2 compares research approaches, identifies disagreements and synthesises findings.

* Data analysis

    Analytics processes extracted experimental results and any user-provided datasets.

* Visualization

    The system generates comparison graphs and research diagrams from verified data.

* Publishing

    The artifact engine generates a structured report with citations, tables, equations and figures.

* Persistent knowledge

    CtxFlow retains validated project knowledge and links it to the generated artifacts.

The result isn't just an answer. It's a traceable research project with sources, knowledge, computations and deliverables.

### 11\. Technical stack — what I'd actually choose

Considering your existing FastAPI, PostgreSQL, Redis, LangGraph, Groq and Ollama direction, I would avoid introducing unnecessary technologies.

Here is my proposed stack.

| Layer | Technology | Priority |
| ------------------ | ----------------------- | ---------------- |
| Backend | FastAPI | Existing |
| Orchestration | LangGraph | Existing |
| System 1 | Laya | Integrate |
| System 2 | Groq + Ollama | Existing |
| Memory | CtxFlow | Core development |
| Database | PostgreSQL | Existing |
| Vector search | pgvector | Existing |
| Cache / PubSub | Redis | Existing |
| Graph processing | NetworkX | Existing/planned |
| Web search | DuckDuckGo + search API | Add |
| Academic discovery | arXiv MCP | Add |
| Metadata | Crossref + OpenAlex | Add |
| PDF extraction | PyMuPDF + GROBID | Add |
| Data processing | Pandas + Polars | Add |
| Analytical SQL | DuckDB | Add |
| Statistics | SciPy + Statsmodels | Add |
| Visualization | Matplotlib + Plotly | Add |
| Documents | Pandoc + Tectonic | Add |
| File storage | Local/object storage | Add |
| Frontend | React | As planned |

I would not add Elasticsearch, a separate graph database, Spark or Kubernetes at this stage.

PostgreSQL, pgvector and NetworkX are sufficient for the initial product. Introduce more infrastructure only when actual scale or workload justifies it.

### 12\. Important architectural principles

There are several things I would make non-negotiable.

A. Separation of intelligence and execution

Laya and System 2 should not have unrestricted access to databases, filesystems or operating-system commands.

They should produce structured requests. Deterministic services validate and execute them.

B. Evidence-first research

Every generated research claim should be traceable to a source or explicitly identified as an inference or hypothesis.

C. Reproducibility

Store:

* Dataset versions.

* Analysis parameters.

* Code and execution versions.

* Source documents.

* Generated chart specifications.

* Document versions.

D. Model independence

ResearchOS should not be dependent on one LLM provider or one particular System 1 checkpoint.

E. Resource-aware operation

Support local inference, selective model invocation, caching, asynchronous processing and token-budget-aware retrieval.

F. Security

Treat downloaded papers, websites and MCP outputs as untrusted inputs. Isolate PDF compilation and analytical code execution, restrict filesystem access, and require permissions for external actions.

### 13\. Development roadmap

This is where I'd be particularly careful. Your project has a relatively small team, and you don't want to build nine incomplete subsystems simultaneously.

I'd divide development into five phases.

* Phase 1 — Core intelligence

    Foundation

    * Integrate Laya.

    * Stabilise CtxFlow.

    * Establish System 1/System 2 interfaces.

    * Implement structured decisions.

    * Establish memory provenance and lifecycle.

    Exit criterion: A working context-management pipeline with measurable evaluation results.

* Phase 2 — Research acquisition

    * Web search.

    * arXiv MCP.

    * Academic metadata APIs.

    * PDF ingestion.

    * Claim extraction.

    * Source registry.

    Exit criterion: A user can discover, retrieve and organise academic papers.

* Phase 3 — Analytical capabilities

    * CSV/XLSX ingestion.

    * Data cleaning and transformations.

    * Statistical analysis.

    * Chart generation.

    * Dataset persistence.

    Exit criterion: A user can upload a dataset and produce reproducible analysis.

* Phase 4 — Document generation

    * Markdown report generation.

    * LaTeX templates.

    * PDF compilation.

    * Bibliography management.

    * Figure and table integration.

    * DOCX/XLSX export.

    Exit criterion: A complete research report can be generated from workspace evidence and analysis.

* Phase 5 — Research workspaces

    * Persistent project environments.

    * Cross-project memory.

    * Experiment history.

    * Artifact versioning.

    * Advanced retrieval.

    * End-to-end research workflows.

    Exit criterion: Multiple independent research projects can be managed without losing context or provenance.

For the current final-year project, I'd prioritise Phase 1 and a narrow vertical slice of Phase 2. The rest is the product roadmap, not something your team needs to finish simultaneously.

### 14\. How we measure whether ResearchOS is actually good

I would establish metrics for each subsystem instead of measuring everything through a single answer-quality score.

| Subsystem | Evaluation metric |
| -------------------- | ---------------------------------------------- |
| Laya | Accuracy, F1, calibration, abstention |
| CtxFlow | Fact F1, retrieval recall, provenance coverage |
| Research acquisition | Search relevance, source coverage |
| Extraction | Claim extraction F1, citation accuracy |
| System 2 | Evidence support, synthesis quality |
| Analytics | Numerical correctness, reproducibility |
| Visualization | Data fidelity, chart correctness |
| Document engine | Compilation success, citation integrity |
| Orchestration | Workflow success, latency, recovery |

For CtxFlow, I'd specifically continue measuring claim-level F1 and retrieval quality. A system that retrieves the wrong context very quickly is not a good memory system.

### 15\. My confidence assessment

I want to distinguish between confidence in the architecture and confidence that the implementation will succeed.

### Architecture confidence

Separation of Laya and System 2

### 98%

Clear division between structured decisions and generative reasoning.

CtxFlow as a dedicated memory infrastructure

### 97%

A strong foundation for persistent, traceable context.

MCP-based research acquisition

### 95%

Good interoperability strategy for external research tools.

Independent analytics and artifact engines

### 97%

Avoids forcing generative models to perform deterministic computation.

Persistent research workspaces

### 96%

A strong long-term product structure.

Entire product achievable by the current team

### 55%

Depends heavily on scope reduction, time and integration effort.

These are my subjective architectural-confidence estimates, not measured probabilities or project-success forecasts.

My strongest confidence is in the separation of responsibilities. My biggest uncertainty is the quality of Laya on your actual memory-management dataset, the performance of the retrieval pipeline and the effort required to integrate all these components.

Those need empirical validation.

### 16\. What I would ultimately want ResearchOS to become

I see three levels of maturity.

Level 1

Research Assistant

* Search papers.

* Read documents.

* Summarise findings.

* Store knowledge.

* Generate reports.

A useful research assistant with persistent memory.

Level 2

Research Workstation

* Independent research workspaces.

* Evidence-backed knowledge graphs.

* Statistical analysis.

* Reproducible experiments.

* Interactive visualisations.

* Publication-ready documents.

An integrated environment for conducting research.

Level 3

Research Operating Environment

* Long-running research projects.

* Continuous knowledge maintenance.

* Proactive discovery of relevant literature.

* Research gap identification.

* Hypothesis and experiment management.

* Cross-project knowledge reuse.

* Reproducible research pipelines.

An environment that helps researchers manage and advance their work over time, with human oversight.

### My final architectural decision

I would freeze the following conceptual boundaries:

| Component | Its identity |
| -------------------- | -------------------------------------------- |
| ResearchOS | The overall research environment |
| Laya | System 1 decision intelligence |
| System 2 | Generative reasoning intelligence |
| CtxFlow | Persistent context and memory infrastructure |
| Research Acquisition | External knowledge discovery |
| Ingestion Engine | Document understanding and extraction |
| Analytics Engine | Data processing and computation |
| Visualization Engine | Visual representation |
| Artifact Engine | Document and file generation |
| MCP Layer | External tool interoperability |
| Orchestrator | Workflow coordination |

And I would make the Research Workspace the central user-facing abstraction.

A workspace brings together the sources, knowledge, data, reasoning history, experiments and deliverables of one research project.

That is what makes all the other components feel like one product rather than a collection of unrelated tools.

One final distinction: ResearchOS should help researchers conduct research, not claim to autonomously establish scientific truth. Human review, source attribution, reproducibility and explicit uncertainty must remain central.

