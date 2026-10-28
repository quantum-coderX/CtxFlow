# CtxFlow / ResearchOS — Complete Repository Handoff Summary

> Purpose: a self-contained description of this repository so another model can generate a
> fresh implementation (or extend/replace this one) without re-reading the code.
>
> Verified against commit `34cf19f`, branch `Adi/feat/v1`, package version `0.2.0`.
> Test status at time of writing: **120 passed, 1 skipped** (`python -m pytest`, 4s).
> The skip is `tests/test_postgres_integration.py` (needs a live PostgreSQL + pgvector).

---

## 1. What this repository is

Two products in one Python distribution named **`ctxflow`**:

1. **`ctxflow`** — a schema-agnostic, incrementally-built, in-memory graph memory engine for
   LLM agents. NetworkX `DiGraph` + inverted tag index. Nodes are "context units"
   (claims, observations, notes, paper references). Edges are typed and directed. Retrieval is
   a weighted blend of tag overlap, recency, and BFS graph proximity, with degree-based
   governance against hub domination. Includes supersession/contradiction detection,
   backward provenance tracing, drop/summarize pruning, and JSON/MessagePack persistence.

2. **`researchos`** — the application layer built on `ctxflow`: a 4-agent pipeline
   (Retriever → Reader → Synthesizer → Critic), a deterministic query router, a schema-validated
   skill registry with 4–5 skills, a FastAPI backend with SSE streaming, a dependency-free
   vanilla-JS "Research Trace" workspace UI, and an offline evaluation harness that compares the
   graph system against two flat-vector-RAG baselines.

There is also a **third, largely separate subsystem**: `ctxflow.memory` — an async
`MemoryStore` protocol with in-memory and PostgreSQL/pgvector implementations, append-only
versioning, provenance rows, and idempotent event ingestion. It shares the package but **is not
wired into the `CtxFlow` facade or into ResearchOS at all**. Treat it as a parallel track.

The project is a Final Year Project. `docs/proposed.md` is the binding scope statement;
`ctxflow_to_researchos_roadmap.md` (v2 plan) and `CODEBASE_LAYERS_AND_OWNERSHIP.md` describe an
**aspirational** target architecture that is only partially implemented (see §12).

---

## 2. Repository map

```
CtxFlow/
├── pyproject.toml                       # package metadata, extras, console scripts, pytest config
├── README.md                            # user-facing quickstart for both ctxflow and researchos
├── CODEBASE_LAYERS_AND_OWNERSHIP.md     # 4-layer spec + per-member ownership + phase plan (32 KB)
├── ctxflow_to_researchos_roadmap.md     # v2 implementation plan, 4 member workstreams (69 KB)
├── docs/
│   ├── proposed.md                      # FYP proposal (authoritative scope / research question)
│   ├── ResearchOS_FYP_Proposal.docx
│   └── *.pdf                            # HippoRAG, Raptor, mem0, GraphReader reference papers
├── migrations/
│   └── 001_memory_schema.sql            # PostgreSQL + pgvector schema for ctxflow.memory
├── src/
│   ├── ctxflow/                         # ── LAYER 1: graph memory engine ──
│   │   ├── __init__.py                  # CtxFlow facade (the public API)
│   │   ├── models.py                    # Node, Edge, CtxFlowConfig, QueryResult, provenance types
│   │   ├── graph.py                     # ContextGraph (NetworkX wrapper, tag index, BFS, provenance walk)
│   │   ├── query.py                     # QueryEngine (composite scoring)
│   │   ├── governance.py                # GovernanceEngine (degree cap, score normalization)
│   │   ├── pruning.py                   # Pruner (drop / summarize compaction)
│   │   ├── extractors.py                # Extractor ABC + DefaultExtractor (rule-based)
│   │   ├── llm_extractor.py             # LLMExtractor (tags, summaries, structured claims)
│   │   ├── contradiction.py             # ContradictionDetector (candidate filter + LLM judge)
│   │   ├── serialization.py             # JSON + MessagePack export/import
│   │   └── memory/                      # ── PARALLEL TRACK: async memory store ──
│   │       ├── __init__.py              # re-exports
│   │       ├── store.py                 # MemoryStore Protocol + Memory/MemoryVersion/Edge/Provenance + enums
│   │       ├── inMemorystore.py         # InMemoryMemoryStore (note the lowercase 'm' filename)
│   │       ├── postgres_store.py        # PostgresMemoryStore (asyncpg)
│   │       ├── ingestion.py             # Event, EventIngestor, IngestResult (idempotent)
│   │       ├── runtime.py               # DatabaseConfig, create_pool, PostgresMemoryRuntime
│   │       ├── embeddings.py            # Embedder Protocol + HashEmbedder
│   │       └── tokens.py                # TokenizerAdapter + SimpleTokenizer + count_tokens
│   └── researchos/                      # ── LAYERS 2–4: application ──
│       ├── __init__.py                  # __version__ = "0.2.0"
│       ├── agents/
│       │   ├── base.py                  # BaseAgent, AgentResult, AgentMessage, AgentStatus
│       │   ├── router.py                # classify_intent + SkillRouter + execution plans
│       │   ├── retriever.py             # RetrieverAgent (internal graph vs external search)
│       │   ├── reader.py                # ReaderAgent (papers → claim nodes)
│       │   ├── synthesizer.py           # SynthesizerAgent (bias-aware packing)
│       │   ├── critic.py                # CriticAgent (validation + standalone verify_claim)
│       │   └── trace.py                 # TraceAgent (provenance exploration, no LLM)
│       ├── skills/
│       │   ├── registry.py              # SkillRegistry + SkillSchema + mini JSON-Schema validator
│       │   ├── graph_query.py           # GraphQuerySkill (query + research_trace + stats)
│       │   ├── literature_search.py     # arXiv + Semantic Scholar + SQLite TTL cache
│       │   ├── paper_reader.py          # PyMuPDF section/claim extraction
│       │   └── review_writer.py         # multi-paper literature review synthesis
│       ├── api/
│       │   ├── main.py                  # console entry: `researchos-api`, module `app`
│       │   ├── server.py                # create_app(), lifespan DI, SSE, health, /api/skills
│       │   ├── schemas.py               # all Pydantic request/response models
│       │   └── routes/{chat,papers,notes}.py   # route modules, each with set_app_state()
│       ├── eval/
│       │   ├── __init__.py              # public exports
│       │   ├── __main__.py              # `researchos-eval` CLI
│       │   ├── dataset.py               # BenchmarkPaper/Question/Suite + default suite
│       │   ├── baselines.py             # HashingEmbedder, FlatVectorStore, 2 baselines
│       │   ├── benchmark.py             # BenchmarkRunner (3 systems)
│       │   └── metrics.py               # EvaluationMetrics + token estimation
│       └── web/                         # bundled static UI (vanilla JS/SVG, no build step)
│           ├── index.html               # 3-panel workspace shell
│           ├── styles.css
│           └── app.js                   # graph layout/render, chat, trace panel, api client
├── tests/                               # 20 files, 121 collected tests
│   ├── llm.py                           # NOT a pytest module — manual NVIDIA NIM demo script
│   └── fixtures/memory_*.json
├── dist/                                # built sdist + wheel (gitignored, present locally)
├── graphify-out/                        # knowledge graph of the repo (gitignored)
├── .agents/                             # editor agent rules for graphify (gitignored)
├── s52_fixed.txt, test456.txt           # local scratch notes (gitignored)
└── .cache/                              # SQLite literature-search cache (gitignored)
```

`pyproject.toml` installs both packages from `src/` (`include = ["ctxflow*", "researchos*"]`),
pins `requires-python >= 3.9`, and marks `researchos/web/*.{html,css,js}` as package data so the
UI ships in the wheel. Pytest is configured with `pythonpath = ["src"]`, `testpaths = ["tests"]`,
and two markers: `postgres` and `researchos`.

Core runtime deps: `networkx>=3.0`, `asyncpg>=0.29`, `python-dotenv>=1.0`, `pydantic>=2.0`.
Extras: `nlp` (spaCy), `binary` (msgpack), `llm` (openai), `researchos` (fastapi, uvicorn,
httpx, pymupdf, sse-starlette), `dev` (pytest + cov + asyncio), `all`.
Console scripts: `researchos-api`, `researchos-eval`.

---

## 3. Layer 1 — `ctxflow` graph memory engine

### 3.1 Data models (`src/ctxflow/models.py`)

All dataclasses (no Pydantic in this layer). IDs are `uuid4().hex[:12]`. Timestamps are
`time.time()` floats (seconds since epoch).

```python
class NodeStatus(str, Enum):
    ACTIVE = "active"; SUPERSEDED = "superseded"; CONTESTED = "contested"

@dataclass
class Node:
    content: str
    id: str = <uuid4 hex[:12]>
    summary: str = ""
    embedding: Optional[List[float]] = None
    tags: Set[str] = set()
    node_type: str = "generic"          # open vocabulary
    timestamp: float = <now>
    metadata: Dict[str, Any] = {}
    last_accessed: float = 0.0
    access_count: int = 0
    status: str = NodeStatus.ACTIVE     # NOTE: annotated `str`, but defaults to the str-enum
    superseded_by: Optional[str] = None
    def touch(self) -> None             # last_accessed = now; access_count += 1

@dataclass
class Edge:
    source_id: str
    target_id: str
    relation_type: str = "related_to"   # open vocabulary
    weight: float = 1.0
    timestamp: float = <now>
    metadata: Dict[str, Any] = {}

@dataclass
class QueryResult:
    node: Node
    score: float = 0.0
    score_breakdown: Dict[str, float] = {}   # keys: tag_overlap, recency, proximity [, contested]

@dataclass
class ContradictionResult:
    existing_node_id: str
    relation: str        # "supersedes" | "contradicts" | "supports" | "unrelated"
    confidence: float = 0.0
    evidence: str = ""

@dataclass
class ProvenanceStep:
    node: Node
    edge: Optional[Edge] = None

@dataclass
class ProvenanceChain:
    root: Node
    steps: List[ProvenanceStep] = []                 # root first, sources last
    contradictions: List[Tuple[Node, Edge]] = []

@dataclass
class ResearchTrace:
    answer_node_id: str
    supporting_chain: ProvenanceChain
    superseded_claims: List[Node] = []
    contradicting_claims: List[Tuple[Node, Edge]] = []
    source_papers: List[Node] = []
```

`CtxFlowConfig` (all defaults shown) with `validate()`:

| Field | Default | Meaning |
|---|---|---|
| `max_link_k` | 5 | top-k tag-similar nodes to auto-link on ingest |
| `min_link_similarity` | 0.1 | minimum Jaccard to create an implicit edge |
| `degree_cap_enabled` | True | enforce max out-degree |
| `degree_cap` | 50 | max **outgoing** edges per node |
| `degree_normalization` | False | divide query score by `(total_degree + 1)` |
| `alpha` | 0.5 | tag-overlap weight |
| `beta` | 0.3 | recency weight |
| `gamma` | 0.2 | proximity weight |
| `bfs_max_depth` | 3 | max hops from anchor |
| `embedding_enabled` | False | **declared but never read** |
| `embedding_model` | "text-embedding-3-small" | **declared but never read** |
| `contradiction_detection` | False | run detection during ingest |
| `contradiction_similarity_threshold` | 0.3 | candidate pre-filter Jaccard |
| `contradiction_confidence_threshold` | 0.7 | min LLM confidence to apply |
| `prune_min_access` | 0 | **declared but never read** |
| `prune_age_weight` | 0.4 | staleness: age component |
| `prune_access_weight` | 0.3 | staleness: access-frequency component |
| `prune_recency_weight` | 0.3 | staleness: since-last-access component |

`validate()` raises `ValueError` if: any of alpha/beta/gamma outside [0,1]; they do not sum to
1.0 (±1e-6); `degree_cap < 1`; `max_link_k < 0`; `bfs_max_depth < 1`.

### 3.2 `ContextGraph` (`src/ctxflow/graph.py`)

Wraps `networkx.DiGraph`; node payload stored as the attribute `data`; edges carry
`relation_type`, `weight`, `timestamp`, `metadata` as edge attributes. Maintains
`_tag_index: Dict[str, Set[str]]` (tag → node IDs) so tag lookup never scans the graph.

Methods (exact surface):

```python
node_count, edge_count                        # properties
add_node(node) -> None                        # O(1); overwrites by id; indexes tags
get_node(node_id) -> Node | None
has_node(node_id) -> bool
remove_node(node_id) -> Node | None           # also cleans tag index + incident edges
all_nodes() -> List[Node]
get_nodes_by_status(status: str) -> List[Node]

add_edge(edge) -> bool                        # False if either endpoint missing; overwrites on same pair
get_edges_from(node_id) -> List[Edge]
get_edges_to(node_id) -> List[Edge]
get_edges_by_type(node_id, relation_type, *, direction="both") -> List[Edge]   # "out"|"in"|"both"
out_degree(node_id) -> int
in_degree(node_id) -> int
total_degree(node_id) -> int
bfs_distances(start_id, max_depth=3) -> Dict[str, int]   # undirected BFS over the DiGraph
trace_provenance(node_id, max_depth=10) -> ProvenanceChain
static jaccard(a: Set[str], b: Set[str]) -> float
find_similar_by_tags(tags, k=5, min_similarity=0.0, exclude=None) -> List[Tuple[Node, float]]
_graph -> nx.DiGraph                          # property used by serialization

_PROVENANCE_EDGE_TYPES = {"follows", "derived_from", "extracted_from", "references"}
_CONTRADICTION_EDGE_TYPES = {"supersedes", "contradicts"}
```

Important semantics:

- `add_edge` overwrites an existing `(source, target)` edge (NetworkX default) — it does **not**
  union by relation type. Two different relations between the same pair cannot coexist.
- `bfs_distances` ignores direction (successors ∪ predecessors).
- `find_similar_by_tags` pre-filters to nodes sharing ≥1 tag, then scores Jaccard and sorts desc.
- `trace_provenance` is a **backward BFS** over `_PROVENANCE_EDGE_TYPES` (incoming edges), and
  collects `supersedes`/`contradicts` edges touching chain nodes in both directions. Steps are
  appended in BFS discovery order (root first), with the connecting edge attached.
  If the root is missing, it returns a `ProvenanceChain` with an empty `Node(content="", id=node_id)`.

### 3.3 `CtxFlow` facade (`src/ctxflow/__init__.py`)

Composes: `ContextGraph`, `Extractor`, `GovernanceEngine`, `QueryEngine`, `Pruner`,
`ContradictionDetector`.

```python
CtxFlow(config: CtxFlowConfig | None = None,
        extractor: Extractor | None = None,
        llm: Callable[[str, str], str] | None = None)
```

Constructor logic:
- `config` defaults to `CtxFlowConfig()`; `config.validate()` always runs.
- extractor selection: explicit `extractor` wins; else if `llm` given →
  `LLMExtractor(llm=llm, fallback=DefaultExtractor())`; else `DefaultExtractor()`.
- `ContradictionDetector(config, llm)` always constructed (inert when `llm is None`).

Properties: `config`, `graph`, `extractor`, `contradiction_detector`.

```python
ingest(content: str, node_type: str | None = None, source_id: str | None = None,
       tags: Set[str] | None = None, metadata: Dict[str, Any] | None = None,
       relation_type: str | None = None, check_contradictions: bool | None = None) -> str

query(context: str, k: int = 5, weights: Tuple[float,float,float] | None = None,
      anchor: str | None = None, include_superseded: bool = False) -> List[QueryResult]

research_trace(node_id: str) -> ResearchTrace
prune(threshold: float = 0.5, strategy: str = "drop") -> List[str]
export(path: str, format: str = "json") -> None      # "json" | "binary"
import_(path: str, format: str = "json") -> None     # replaces graph, rebuilds engines
__repr__ -> "CtxFlow(nodes=N, edges=M)" ; __len__ -> node_count
```

**`ingest` algorithm (the hot path, O(1)-ish):**

1. `tags = extractor.extract_tags(content) | caller_tags`; `summary = extractor.summarize(content)`.
2. Create `Node` with `node_type or "generic"`, `metadata or {}`; `graph.add_node(node)`.
3. If `source_id` exists **and** is present in the graph **and** governance allows →
   add `Edge(source_id → node.id, relation_type or "follows", weight=1.0)`.
   *(Silently skipped if the source is missing or capped — no error.)*
4. If `config.max_link_k > 0`: fetch top-k tag-similar nodes (excluding self, min similarity),
   and for each, if `governance.can_add_edge(graph, node.id)` →
   add `Edge(node.id → similar.id, "related_to", weight=jaccard)`.
5. If `check_contradictions` (or config default) → `contradiction_detector.detect_and_apply(graph, node)`.
6. Return `node.id`.

Note: step 4 always links **out of the new node**; the source node's own cap is never consulted.

**`research_trace(node_id)`:** builds `ProvenanceChain` via `graph.trace_provenance`, then splits
chain nodes into `superseded_claims` (status superseded) and `source_papers`
(`node_type in {"paper", "reference", "source"}`). `contradicting_claims` is the chain's
contradiction list.

### 3.4 `QueryEngine` (`src/ctxflow/query.py`)

Stateless. `query(graph, context, k=5, weights=None, anchor=None, include_superseded=False)`:

1. `query_tags = extractor.extract_tags(context)`.
2. If `anchor` exists in graph → `bfs_distances(anchor, max_depth=config.bfs_max_depth)`.
3. Score **every** node (full scan, O(N)):
   - `tag_overlap = jaccard(query_tags, node.tags)`
   - `recency = 1 / (1 + (now - node.timestamp))` — seconds directly, so all but
     sub-second-old nodes score ≈ 0.
   - `proximity = 1/(1+hops)` if reachable from anchor; `1.0` for the anchor itself;
     `0.0` when no anchor or unreachable.
   - `score = α·tag + β·recency + γ·proximity`
   - `score = governance.normalize_score(score, graph.total_degree(node.id))`
   - `breakdown = {"tag_overlap", "recency", "proximity"}`, plus `"contested": 1.0` when the node
     status is `contested`.
   - Superseded nodes are skipped unless `include_superseded=True`.
4. Sort desc, take `k`.
5. `touch()` every returned node (mutates access tracking).

Known weakness worth fixing in a reimplementation: `recency` uses raw seconds, so no anchor and
no tag overlap → every node ties at ~0 and ordering is insertion-dependent.

### 3.5 `GovernanceEngine` (`src/ctxflow/governance.py`)

```python
can_add_edge(graph, source_id) -> bool      # False when out_degree >= degree_cap (or cap disabled)
normalize_score(score, degree) -> float     # score / (degree + 1) when enabled, else score
```

### 3.6 `Pruner` (`src/ctxflow/pruning.py`)

`prune(graph, threshold=0.5, strategy="drop") -> List[str]` — mutates the graph in place.
Raises `ValueError` for an unknown strategy.

Staleness per node (`_staleness`), higher = more stale:

```
max_age      = max(now - oldest_timestamp, 1.0)
age_score    = min((now - node.timestamp) / max_age, 1.0)
access_score = 1 / (1 + node.access_count)
recency_score= min((now - node.last_accessed)/max_age, 1.0) if last_accessed > 0 else 1.0
staleness    = w_age·age_score + w_access·access_score + w_recency·recency_score
```

Nodes with `staleness > threshold` are stale.

- `"drop"` → `graph.remove_node` each stale node. **No edge healing** (edges die with the node;
  this differs from the proposal text which mentions healing).
- `"summarize"` → find connected components **within the stale subgraph**, and for each:
  collect non-stale neighbours (both directions), build a summary node
  (`node_type="summary"`, `metadata["compacted_from"] = [ids]`, tags = union of cluster tags,
  content from `extractor.summarize_nodes`), remove the stale nodes, insert the summary, and link
  it to each surviving neighbour with `Edge(summary → nbr, "summarizes", 1.0)`.
  Returns only the removed IDs (summary node IDs are not returned).

### 3.7 `ContradictionDetector` (`src/ctxflow/contradiction.py`)

Two-stage pipeline; the LLM is injected as a `Protocol`:

```python
class LLMCallable(Protocol):
    def __call__(self, prompt: str, system: str = "") -> str: ...
```

```python
enabled -> bool                      # llm is not None AND config.contradiction_detection
find_candidates(graph, new_node, *, max_candidates=10) -> List[Node]
    # tag-Jaccard pre-filter above contradiction_similarity_threshold, ACTIVE nodes only
check_contradictions(new_node, candidates) -> List[ContradictionResult]
    # one LLM call per candidate; drops relation == "unrelated"
apply_supersession(graph, new_node_id, old_node_id, result) -> Edge | None
    # Edge(new → old, result.relation, weight=confidence,
    #      metadata={"confidence", "evidence"})
    # relation "supersedes" → old.status = SUPERSEDED, old.superseded_by = new
    # relation "contradicts" → old.status = CONTESTED
detect_and_apply(graph, new_node, *, max_candidates=10) -> List[ContradictionResult]
    # full pipeline, applies only results with confidence >= threshold
```

Prompt contract: system prompt demands strict JSON
`{"relation": ..., "confidence": float, "evidence": "..."}` with allowed relations
`supersedes | contradicts | supports | unrelated`. `_parse_response` strips ``` fences, validates
the relation (falls back to `"unrelated"`), and clamps confidence to [0,1].
Any exception per pair is logged and skipped (returns `None`).

### 3.8 Extractors

`Extractor` ABC (`src/ctxflow/extractors.py`) — exactly three abstract methods:

```python
extract_tags(text: str) -> Set[str]
summarize(text: str) -> str
summarize_nodes(nodes: List[Node]) -> str
```

`DefaultExtractor(max_tags=20, max_summary_sentences=3, stop_words=None)`:
- tags = lowercase words of ≥3 letters, minus a built-in ~65-word stop list, ranked by frequency,
  capped at `max_tags`, returned as a set.
- summary = first 3 sentences (regex split on `[.!?]` + whitespace).
- node summary = `" | ".join(summary-or-first-100-chars)`.

`LLMExtractor(llm, max_tags=20, fallback=None)` (`src/ctxflow/llm_extractor.py`):
- `extract_tags` asks for a JSON array of lowercase strings; on failure → fallback, else naive
  whitespace tokens.
- `summarize` asks for 1–3 sentences; on failure → fallback, else `text[:200]`.
- `summarize_nodes` builds a numbered `[i] (type): text` block and asks for one synthesis.
- **Extended method** `extract_claims(text) -> List[Dict]` — not part of the ABC. Returns
  `[{type, text, confidence, tags}]` with `type ∈ {claim, method, result, finding, hypothesis}`.
  On failure returns the whole text as a single low-confidence claim.
- Both parsers strip markdown fences and coerce types/clamp confidence.

### 3.9 Serialization (`src/ctxflow/serialization.py`)

Payload = `{"version": 1, "nodes": [...], "edges": [...]}`; tags are sorted for determinism.
Edges are gathered by iterating all nodes and taking `get_edges_from`.

```python
export_json(graph, path) ; import_json(path) -> ContextGraph
export_binary(graph, path) ; import_binary(path) -> ContextGraph   # msgpack, raises ImportError w/o extra
```

---

## 4. Parallel track — `ctxflow.memory` (async store)

Not connected to `CtxFlow`/ResearchOS. Async, protocol-based, production-oriented.

### 4.1 Enums and records (`memory/store.py`)

```python
MemoryType:  conversation, fact, decision, observation, tool_result, document, task,
             correction, summary, user_preference
MemoryTier:  working, episodic, semantic          # three views over ONE memories table
ValidityState: valid, stale, contradicted, requires_review, superseded
Relation:    derived_from, depends_on, based_on, summary_of, caused_by, supersedes,
             contradicts, duplicate_of, follows, related_to
SourceType:  user_message, assistant_message, tool_output, document, derived, external
```

```python
@dataclass Memory:                       # current, denormalized view
    text; id=<uuid4 hex[:16]>; memory_type=CONVERSATION; tier=EPISODIC;
    created_at=<aware UTC>; session_id=None; source=""; importance=0.0; access_count=0;
    current_version=1; validity=VALID; archived=False; entities=[]; embedding_model="";
    embedding: list[float]=[]; token_counts: dict[str,int]={}

@dataclass(frozen=True) MemoryVersion:
    memory_id, version, text, embedding, created_at, reason, supersedes_version, actor

@dataclass(frozen=True) Edge:            # memory-graph edge — distinct from ctxflow.models.Edge
    src, dst, relation: Relation, weight=1.0, confidence=1.0, created_by="rule", created_at

@dataclass(frozen=True) Provenance:
    memory_id, source_type: SourceType, source_id=None, author=None, tool=None,
    extraction_confidence=1.0, parent_memories=[], created_at

@dataclass(frozen=True) ScoredId:  memory_id, score
@dataclass(frozen=True) Subgraph:  nodes: list[Memory], edges: list[Edge]
```

`MemoryStore` Protocol:

```python
add(memory) -> str
get(memory_id, version=None, as_of=None) -> Memory | None
create_version(memory_id, new_text, reason, supersedes_version, actor, *, embedding=None) -> int
search(query_emb, query_text, k, *, types=None, include_archived=False,
       include_history=False, as_of=None) -> list[ScoredId]
neighbors(memory_id, *, depth, relations=None, direction="both",
          max_nodes=200, fanout_cap=20) -> Subgraph
get_version_history(memory_id) -> list[MemoryVersion]
```

Implementations additionally expose `add_edge`, `add_provenance`, and (Postgres/ingest path)
`claim_event` / `persist_ingestion`.

### 4.2 `InMemoryMemoryStore`

Dicts keyed by id, plus `versions`, `edges[(src,dst,relation)]`, `provenance`.
- `add` is idempotent and writes a version-1 snapshot (`reason="initial"`, `actor="system"`).
- `get` with no version/as_of returns a copy; otherwise picks the highest matching version
  (`version ==` or `created_at <= as_of`) and overlays text/embedding/current_version.
- `create_version` appends, validates `supersedes_version` must equal the current version,
  reuses the old embedding when none is given, and bumps the current pointer.
- `search` fuses keyword and dense signals with **Reciprocal Rank Fusion** (`1/(60+rank)` summed
  over both ranked lists); no-embedding and no-terms cases fall through.
- `neighbors` = bounded BFS with `fanout_cap` (edges sorted by `-weight`), `max_nodes` budget,
  direction filter, relation filter; returns edges + nodes including the start node.
- `add_provenance` **overwrites** any existing provenance for that memory (one row per memory).

### 4.3 `EventIngestor` (`memory/ingestion.py`)

```python
@dataclass(frozen=True) Event: event_id, session_id, ts, role, content,
                               tool_name=None, metadata={}
@dataclass(frozen=True) IngestResult: event_hash, memory_ids, duplicate=False
```

Flow: `event_hash = sha256(event_id)`; if seen → `duplicate=True`.
`memory_id = sha256(f"{event_hash}:{role}:{content}")[:16]`.
Classifies (`tool_name` → TOOL_RESULT; "remember that"/"i prefer" → USER_PREFERENCE;
text starting "decided"/"decision:"/"we will" → DECISION; else CONVERSATION), embeds, builds a
`FOLLOWS` edge from the previous memory in the same session, builds `Provenance`
(TOOL_OUTPUT vs USER_MESSAGE).
Persistence prefers `store.persist_ingestion(...)` (atomic, returns bool = claimed); otherwise
falls back to `claim_event` + `add` + `add_edge` + `add_provenance`.
Keeps in-process `_seen`, `_events`, `_last_by_session` maps.

### 4.4 `PostgresMemoryStore` (`memory/postgres_store.py`)

asyncpg pool + SQL. Tables: `ingestion_events`, `memories`, `memory_versions`, `graph_edges`,
`provenance`; `embedding VECTOR(128)`; HNSW cosine index. See
`migrations/001_memory_schema.sql`.

- Optional `embedding_dimension` guard validates vector length on write and read.
- Vectors are serialized as `"[v1,v2,...]"` strings for pgvector parameters.
- `search` has four SQL variants: (no vector | vector) × (as_of | current). Vector path uses
  `1 - (embedding <=> $1::vector)` and `ORDER BY embedding <=> ...`; the text-only path is a
  plain `ILIKE '%query%'` with `score 0.0`; both filter archived/types/validity.
  `as_of` uses a `LATERAL` subselect over `memory_versions`.
- `neighbors` does level-by-level BFS with per-node SQL queries (`LIMIT fanout_cap`).
- `create_version` locks the row `FOR UPDATE`, validates the supersede pointer, inserts, updates.
- `persist_ingestion` claims the event, then inserts memory + version + optional edge + provenance
  in one transaction; returns `False` when the event was already claimed.
- Row→model helpers convert pgvector strings to float lists.

### 4.5 `runtime.py` / `embeddings.py` / `tokens.py`

- `DatabaseConfig(host, port, database, user, password)`; `load_database_config(dotenv_path=None)`
  reads `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` via `dotenv_values`, validates presence and
  port range 1–65535.
- `create_pool(config=None, *, dotenv_path=None, **pool_options)` → asyncpg pool from **separate
  fields** (never a URL string).
- `PostgresMemoryRuntime.create(embedder=None, dotenv_path=None, **pool_options)` builds pool +
  `PostgresMemoryStore(pool, embedding_dimension=128)` + `EventIngestor`, default
  `HashEmbedder(dimension=128)`; `close()`; async context manager.
- `HashEmbedder(dimension=128, model="hash-v1")` — blake2b signed hashing trick, L2-normalized.
  `embed_text(embedder, text)` validates returned dimension.
- `SimpleTokenizer` counts `\w+|[^\w\s]`; `count_tokens(memory, tokenizer)` caches into
  `memory.token_counts[tokenizer.tokenizer_id]`.

---

## 5. Layer 2 — Skills (`src/researchos/skills/`)

### 5.1 `SkillRegistry` (`registry.py`)

Pydantic v2 models `SkillInputSchema` / `SkillOutputSchema` (extra allowed), plus:

```python
class SkillSchema(BaseModel):
    name: str; description: str = ""; input_schema: dict = {}; output_schema: dict = {};
    tags: list[str] = []; version: str = "0.1.0"

class SkillResult(BaseModel):
    skill_name: str; success: bool = True; data: Any = None; error: str | None = None;
    metadata: dict = {}
```

Registry API:

```python
register(name, description="", input_schema=None, output_schema=None, tags=None,
         version="0.1.0") -> decorator          # detects async via asyncio.iscoroutinefunction
register_function(name, fn, description="", input_schema=None, output_schema=None,
                  tags=None, version="0.1.0") -> None
list_skills() -> List[SkillSchema]
get_schema(name) -> SkillSchema | None
has_skill(name) -> bool
find_by_tag(tag) -> List[SkillSchema]
invoke(name, **kwargs) -> SkillResult          # sync; refuses async skills
invoke_async(name, **kwargs) -> SkillResult    # awaits async, runs sync in default executor
__len__ ; __contains__ ; __repr__
```

`validate_input(schema, payload) -> str | None` is a **deliberately minimal** JSON-Schema subset:
`type` (incl. type lists), `required`, `properties.*.type`, `minimum`, `enum`. Booleans are
rejected for `integer`/`number`. Unknown keywords are ignored.

### 5.2 `GraphQuerySkill` (`graph_query.py`)

Wraps the facade. Dataclasses `GraphQueryResult(node_id, content, summary, score, node_type,
status, tags, score_breakdown)` and `TraceResult(answer_node_id, provenance_chain,
superseded_claims, contradictions, source_papers)` — all serialized to plain dicts/lists.

```python
query(query_text, k=5, anchor=None, include_superseded=False, token_budget=None)
    # over-fetches k*2 when token_budget is set; greedy 4-chars/token packing
research_trace(node_id) -> TraceResult
get_graph_stats() -> {total_nodes, total_edges, status_distribution, type_distribution}
```

### 5.3 `LiteratureSearchSkill` (`literature_search.py`)

Pure `urllib` (no requests/httpx). Providers: arXiv Atom API
(`http://export.arxiv.org/api/query`) and Semantic Scholar Graph API
(`https://api.semanticscholar.org/graph/v1/paper/search`).

- `PaperResult(title, authors, summary/abstract, year, doi, arxiv_id, url, source, metadata)`.
- SQLite TTL cache at `f"{cache_dir or '.cache'}/literature_cache.db"`, table
  `cache(key TEXT PRIMARY KEY, value TEXT, ts REAL)`, default TTL 7 days, key =
  `sha256("provider:normalized_query:max_results")`.
- `search(query, max_results=5, provider="both")` concatenates providers and dedupes by
  lowercased title.
- `_parse_arxiv_xml` (ElementTree + Atom namespace) and `_parse_s2_response`.
- `clear_cache()`, `close()`. Network/parse failures log and return `[]` (never raise).
- Retrieval swallows nothing else: no retries, no rate-limit handling, no API keys.

### 5.4 `PaperReaderSkill` (`paper_reader.py`)

Requires PyMuPDF (`fitz`); raises `ImportError` with an install hint if absent.

- `extract_text_from_pdf(path) -> str`; `extract_sections_from_pdf(path) -> List[PaperSection]`
  using a font-size heuristic (span size ≥ 12, line < 100 chars, no trailing "."), with a
  full-document fallback section.
- `PaperReaderSkill(llm=None, chunk_size=3000)`:
  `read_pdf(path) -> {title, sections, full_text, page_count}`;
  `extract_claims(path) -> List[ExtractedClaim]` — per section, LLM chunked claim extraction if
  an LLM is present, else sentence-level heuristic typing
  (result/method/finding/hypothesis/claim by keyword) with up to 5 keyword tags.
- `ExtractedClaim(claim_type, text, confidence, tags, source_section, page_number)`.

### 5.5 `ReviewWriterSkill` (`review_writer.py`)

`write_review(topic, max_sources=20, include_superseded=False, token_budget=4096)`:
query the graph → bucket nodes into claims / methods / findings / papers → bias-aware reorder
(critical items at front and back) → per-section LLM generation or heuristic bullet lists →
abstract (LLM or first-sentence concatenation) → references from node metadata.
Returns `LiteratureReview(title, abstract, sections[ReviewSection(heading, content, cited_nodes)],
references, total_papers)`. Prompt asks for `[Node:ID]` citations and flags
`[SUPERSEDED]` / `[CONTESTED]`.

---

## 6. Layer 3 — Agents (`src/researchos/agents/`)

### 6.1 `BaseAgent` (`base.py`)

```python
class AgentStatus(str, Enum): IDLE, RUNNING, COMPLETED, FAILED

@dataclass AgentMessage: sender, content, message_type="data", metadata={}, timestamp
@dataclass AgentResult: agent_name, success=True, data=None, error=None, messages=[],
                        metadata={}, duration_ms=0.0
```

`BaseAgent(name, llm=None)`; `run(**kwargs)` sets RUNNING, times execution, calls
`execute(**kwargs)`, normalizes non-`AgentResult` returns into one, stamps `duration_ms` and
`agent_name`, and **catches all exceptions** into `AgentResult(success=False, error=str(exc))`.
`execute` is abstract. Properties `status`, `execution_count`.

### 6.2 `SkillRouter` (`router.py`)

Rule-based intent classification (no LLM), `classify_intent(query) -> QueryIntent` where intents
are `NEW_LITERATURE`, `EXISTING_KNOWLEDGE`, `CLAIM_VERIFICATION`, `EXPLORATORY_TRACE`, `UNKNOWN`.
Each intent owns a list of regex patterns; the intent with the most pattern hits wins.

Execution plans:

| Intent | Pipeline |
|---|---|
| `NEW_LITERATURE` | retriever → reader → synthesizer → critic |
| `EXISTING_KNOWLEDGE` | synthesizer → critic |
| `CLAIM_VERIFICATION` | critic |
| `EXPLORATORY_TRACE` | graph_query (the `TraceAgent`, registered under that name) |
| `UNKNOWN` | synthesizer → critic |

`SkillRouter(agents: Dict[str, BaseAgent] | None, llm=None)`, `register_agent(name, agent)`,
`execute(query=..., context=...)`:
for each planned agent name, skip unregistered agents (logged), call
`agent.run(query=..., context=current_data, previous_results=pipeline_results)`, append the
result, continue on failure, and thread `current_data = {"query", "previous_output", **context}`.
Final `AgentResult.data = {"intent", "plan", "result": <last agent data>, "pipeline_results": [
{agent, success, duration_ms, error}]}` with metadata `{intent, plan, agents_executed}`.

### 6.3 The four agents (+ trace agent)

| Agent (name) | Constructor | Core behaviour |
|---|---|---|
| `RetrieverAgent` (`"retriever"`) | `(ctxflow, literature_search=None, min_internal_results=2, min_score_threshold=0.3, llm=None)` | Graph query top-k → knowledge-gap test (`len < min` **or** `avg_score < threshold`) → external search skill. Data: `{internal_results[], knowledge_gap, external_results[]}`. |
| `ReaderAgent` (`"reader"`) | `(ctxflow, llm=None, paper_reader=None)` | Pulls `external_results` from the retriever's result (or `context["previous_output"]`). Per paper: optional PDF path → `PaperReaderSkill`, else LLM/heuristic abstract claims. Ingest a `paper` node, then each claim as `Edge(paper → claim, "extracted_from")` with `check_contradictions=True`. Data: `{ingested_papers[], total_nodes_created}`. |
| `SynthesizerAgent` (`"synthesizer"`) | `(ctxflow, llm=None, token_budget=2048)` | `query(k=20, include_superseded=False)` → bias-aware reorder (score > 0.6, contested, or has `superseded_by` are high-salience; split half front / half back, normal in middle) → greedy pack at 4 chars/token → build `[Node:ID][STATUS] (type, score)` blocks → LLM synthesis or bullet fallback. Data: `{answer, cited_nodes, alerts}`. Divider between nodes: `"\n\n---\n\n"`. |
| `CriticAgent` (`"critic"`) | `(ctxflow, llm=None)` | Takes `cited_nodes`/`answer` from the synthesizer (or `previous_output`). If none (CLAIM_VERIFICATION route) → `verify_claim(query)`. Per cited node: `_validate_node` → `research_trace`, supersessions (own + chain), contradictions, source-paper count, chain length. Verdict `"VERIFIED"` only if zero supersession and zero contradiction warnings, else `"WARNINGS_FOUND"`. Data includes `validations`, `*_warnings`, `total_claims_checked`, `alerts`. |
| `TraceAgent` (`"graph_query"`) | `(ctxflow, k=5, llm=None)` | No LLM. Query with `include_superseded=True`, take the top hit, `research_trace` it, emit alerts for superseded/contradicting nodes and a sentence answer. Data: `{answer, cited_nodes, alerts, trace}`. |

`CriticAgent.verify_claim(claim_text, node_id=None)` is a public standalone API returning
`{claim, supporting_evidence, contradicting_evidence, superseded_evidence, confidence, trace}`,
where `confidence = |supporting| / (|supporting| + |contradicting|)`.

Note: `ReaderAgent` and `TraceAgent` are the only agents that write to the graph / use traces;
the router's `graph_query` key maps to `TraceAgent` (name collision with the skill name).

---

## 7. Layer 4 — API and UI

### 7.1 `create_app` (`api/server.py`)

`create_app(ctxflow=None, skill_registry=None, router_agent=None, enable_cors=True) -> FastAPI`.
Title "ResearchOS API", version `0.2.0`. A `lifespan` context manager builds state:

1. `CtxFlow()` if not injected (in-memory, `DefaultExtractor`, no LLM, contradiction detection off).
2. Skills: `LiteratureSearchSkill()`, `PaperReaderSkill()`, `GraphQuerySkill(ctxflow)`,
   `ReviewWriterSkill(ctxflow)`.
3. Registry populated with **five** functions: `literature_search`, `paper_reader`, `graph_query`,
   `research_trace`, `review_writer` (each with an input schema, tags, output schema).
4. Agents: `retriever`, `reader`, `synthesizer`, `critic`, `graph_query` (→ `TraceAgent`) wrapped
   in `SkillRouter`.
5. `set_app_state(state)` injected into each route module (module-level global dicts).
6. Teardown closes the literature-search SQLite connection.

CORS is permissive (`allow_origins=["*"]`). Static UI is mounted **last** at `/` so
`/api`, `/docs`, `/openapi.json` win.

Endpoints:

| Method | Path | Handler module | Notes |
|---|---|---|---|
| GET | `/health` | server.py | `{status, version, graph_nodes, graph_edges}` |
| GET | `/api/skills` | server.py | registry listing with schemas |
| GET | `/api/stream?query=` | server.py | SSE via `sse_starlette`; events `start`, `agent_completed` (one per pipeline agent), `done`; router run in an executor |
| POST | `/api/chat` | routes/chat.py | `ChatRequest{query, session_id?, include_trace=False, token_budget=2048}` → `ChatResponse{answer, intent, cited_nodes, alerts, trace?, metadata}`; unpacks `data["result"]`; optional trace summary for `cited_nodes[0]` |
| POST | `/api/query` | routes/chat.py | direct `ctxflow.query` → ranked results |
| POST | `/api/search` | routes/chat.py | external literature search |
| POST | `/api/papers/ingest` | routes/papers.py | metadata + abstract/content; splits abstract into sentences (≥20 chars), each a `claim` node extracted from the paper node; contradiction count inferred from superseded delta |
| POST | `/api/papers/upload` | routes/papers.py | multipart PDF → temp file → PaperReader → claims; 503 on missing PyMuPDF; temp file always deleted |
| GET | `/api/graph/stats` | routes/papers.py | node/edge totals + status/type distributions |
| GET | `/api/graph/data` | routes/papers.py | nodes (content truncated to 500) + edges, for the visualizer |
| POST | `/api/trace` | routes/papers.py | full `TraceResponse` for one node |
| POST | `/api/notes` | routes/notes.py | note stored as a `note` node with `project_id`, title prefixed into content |
| GET | `/api/notes?project_id=default` | routes/notes.py | notes filtered by project, newest first |
| GET | `/` | static mount | workspace UI |

All routes catch exceptions and raise `HTTPException(500, detail=str(exc))`; missing state →
`503`. Pydantic schemas live in `api/schemas.py` (chat, paper ingest, node/edge/graph, trace,
query, search, notes, skill, health). `api/main.py` exposes module-level `app = create_app()` and
`main()` reading `RESEARCHOS_HOST` (127.0.0.1), `RESEARCHOS_PORT` (8000), `RESEARCHOS_RELOAD`.

### 7.2 Workspace UI (`web/`)

No build step, no npm, no framework. `index.html` is a 3-panel layout:
**Library** (paper form, PDF upload, note form, paper/note lists), **Chat** (message stream,
composer, intent badge), and **Knowledge graph** (SVG canvas + Research Trace panel).
Top bar shows node/edge counts, health dot, and a project-id input.

`app.js` implements: a `state` object, DOM helpers, `api()`/`jsonRequest()` fetch wrappers,
`loadHealth`/`loadNotes`/`loadGraph`, `renderPapers`, `NODE_COLORS`/`nodeColor` by node type,
a hand-rolled force/circular `layoutGraph`, `renderGraph` (SVG nodes + edges, click → trace),
`showTrace(nodeId)` rendering provenance steps, superseded claims, contradictions and source
papers, `addMessage`/`askQuestion` (POST `/api/chat`, renders citations that open the trace),
and a toast. It does **not** use the SSE endpoint.

---

## 8. Evaluation harness (`src/researchos/eval/`)

Purpose: prove the central research claim — graph memory with supersession edges beats flat
vector RAG at reflecting up-to-date claims and at citing provenance — offline and deterministically.

### 8.1 Data (`dataset.py`)

```python
@dataclass BenchmarkPaper:    id, title, year, claims: List[dict], abstract=""
@dataclass BenchmarkQuestion: id, question, expected_answer,
                              expected_active_claims: List[str],
                              expected_superseded_claims: List[str],
                              requires_contradiction_awareness=True,
                              ground_truth_sources: List[str] = []
@dataclass BenchmarkSuite:    name, papers, questions
get_default_contradiction_benchmark() -> BenchmarkSuite
```

Default suite ("Scientific Contradiction & Supersession Suite v1"): p1 (2020) Algorithm X 85%,
p2 (2022) corrects to 71% with `supersedes` pointer, p3 (2023) Algorithm Z 89%; questions q1
(verified accuracy of X → 71%, ground truth p2, superseded 85% claim) and q2 (best valid accuracy
→ Z 89%, ground truth p3). Items 4/5 described in the docstring are not actually constructed.

### 8.2 Baselines (`baselines.py`)

- `HashingEmbedder(dimensions=256)`: blake2b signed hashing trick, L2-normalized, plus `cosine`.
- `FlatDocument(id, text, metadata, embedding)`; `FlatHit(document, score)` with `.text`/`.metadata`.
- `FlatVectorStore(embedder=None, k=5)`: `ingest`, `ingest_paper` (paper + per-claim chunks),
  `index_suite`, `search(query, k=None)` → cosine top-k. Alias `FlatRAGBaseline`.
- `SingleAgentFlatRAG` (Baseline A): top-k retrieval, answer = best text, `traceable=False`.
- `MultiAgentFlatRAG` (Baseline B): over-fetch `k*2`, "reader" normalizes chunks, "synthesizer"
  picks the best, "critic" explicitly reports that no provenance is available; `traceable=False`.

### 8.3 Metrics (`metrics.py`)

`CHARS_PER_TOKEN = 4`; `estimate_tokens(text) = max(1, ceil(len/4))`;
`text_matches` (normalized substring), `matching_claims`.

`EvaluationMetrics` counters: `total_questions`, `questions_correct`,
`correct_active_claims_retrieved`, `superseded_claims_suppressed`,
`superseded_claims_incorrectly_returned`, `provenance_traces_found`, `provenance_correct`,
`context_tokens_total`.

Derived: `answer_accuracy`, `provenance_accuracy`, `precision_against_supersession`,
`supersession_defense_rate`, `avg_context_tokens` (alias `token_cost_per_query`).

`record(question, retrieved_texts, cited_sources=None, context_text="", trace_found=False)`:
a question is correct when the active claim is retrieved **and** no superseded claim appears
(or, if no active claim is expected, when no superseded claim appears). Provenance credit
requires correctness **and** an intersection with `ground_truth_sources`.
`to_dict()` rounds rates to 4 dp for JSON.

### 8.4 Runner (`benchmark.py`) and CLI

`BenchmarkRunner(suite, k=5)`:
- `run_benchmark() -> {"ctxflow_graph", "flat_rag", "flat_rag_multi_agent"}`.
- `run_report() -> {suite, questions, papers, k, systems: {name: metrics.to_dict()}}`.
- `format_report()` → markdown table with the four headline metrics.
- `_evaluate_ctxflow()` builds a fresh `CtxFlow(contradiction_detection=False)`, ingests papers and
  claims with `extracted_from` edges, then **manually** creates `supersedes` edges from the
  dataset's `supersedes` text pointers and flips old nodes to `SUPERSEDED`
  (i.e. the LLM detector is bypassed; supersession is ground-truth injected), queries with
  `include_superseded=False`, and derives sources from traces plus node metadata.
- `_evaluate_flat_rag(cls)` indexes the suite into the chosen baseline and records answers.

CLI: `researchos-eval` / `python -m researchos.eval` with `--json`, `--out <path>`, `--k 5`.

---

## 9. Tests (`tests/`, 121 collected, 120 pass / 1 skip)

| File | Focus |
|---|---|
| `test_models.py` | Node/Edge/Config validation (`TestNode`, `TestEdge`, `TestCtxFlowConfig`, `TestQueryResult`) |
| `test_graph.py` | node ops, edge ops, BFS, tag retrieval (24 tests) |
| `test_query.py` | scoring components, degree normalization, superseded filtering |
| `test_governance.py` | degree cap, score normalization |
| `test_pruning.py` | drop + summarize strategies |
| `test_extractors.py` | DefaultExtractor tags/summary/stop words |
| `test_serialization.py` | JSON + MessagePack round trips |
| `test_integration.py` | end-to-end facade flows |
| `test_memory_vertical_slice.py` | event ingestion idempotency, session edge, neighbors caps, token caching |
| `test_postgres_runtime.py` | dotenv config validation, pool field passing, runtime wiring (mocked) |
| `test_postgres_integration.py` | **skip** unless a real DB is configured (`postgres` marker) |
| `test_agents.py` | router classification/plans, retriever gap logic, reader, synthesizer, critic |
| `test_skills.py` | registry validation/invocation, lit search parsing/cache, graph query, paper reader, review writer |
| `test_api.py` | FastAPI endpoints via TestClient |
| `test_workspace_api.py` | UI assets served, notes CRUD, routing agents, paper metadata |
| `test_eval.py` / `test_eval_harness.py` | metrics math, baselines, runner determinism |

`tests/llm.py` is a **manual script**, not a test module: it compares a 3-turn sliding-window
baseline against CtxFlow retrieval using an NVIDIA NIM OpenAI-compatible endpoint
(`NVIDIA_API_KEY`, model `meta/llama-3.2-11b-vision-instruct`). It executes network calls at import
time, so it must never be collected.

There is no `conftest.py`; `pythonpath` comes from `pyproject.toml`. `tests/fixtures/*.json`
are small memory-graph fixtures.

---

## 10. Contracts another implementation must preserve

If the goal is a drop-in reimplementation, keep these exact behaviours:

1. **Public facade names**: `CtxFlow.ingest/query/research_trace/prune/export/import_`, plus
   properties `config`, `graph`, `extractor`, `contradiction_detector`.
2. **Model field names/defaults**: `Node.{id,content,summary,embedding,tags,node_type,timestamp,
   metadata,last_accessed,access_count,status,superseded_by}`, `Edge.{source_id,target_id,
   relation_type,weight,timestamp,metadata}`.
3. **Scoring formula and weights**: `α·Jaccard(tags) + β·1/(1+age_s) + γ/(1+hops)`, defaults
   0.5/0.3/0.2, summing to 1.0, optional `/(degree+1)`.
4. **Relation vocabulary**: implicit `related_to`, default explicit `follows`, extraction
   `extracted_from`, compaction `summarizes`, truth-maintenance `supersedes` / `contradicts` /
   `supports`. Provenance walk respects only `follows|derived_from|extracted_from|references`.
5. **Status lifecycle**: `active → superseded` (with `superseded_by`) on `supersedes`;
   `active → contested` on `contradicts`; superseded nodes excluded from default retrieval.
6. **LLM boundary**: a plain callable `(prompt, system="") -> str`, injected everywhere; no
   provider import in library code. All LLM failures must degrade gracefully (fallback extractor,
   skipped contradiction pair, heuristic synthesis).
7. **Extractor ABC**: exactly `extract_tags`, `summarize`, `summarize_nodes`.
8. **Agent contract**: subclasses implement `execute(**kwargs) -> AgentResult`; `run()` owns
   timing/error capture; failures never raise out of `run()`.
9. **Skill contract**: `SkillSchema` + `SkillResult`; registration by decorator or function;
   validation returns an error string, invocation never raises.
10. **API shapes**: the Pydantic models in `api/schemas.py` and the endpoint table in §7.1.
11. **Eval contract**: four headline metrics and the `{"systems": {name: metrics}}` report shape.

---

## 11. Known gaps, inconsistencies, and traps

Fix or consciously preserve these in any new implementation:

1. **Two disconnected memory systems.** `CtxFlow` (NetworkX, sync, in-process) and
   `ctxflow.memory` (async protocol, Postgres) share nothing. The Postgres store is never used by
   ResearchOS, so all API data is lost on restart. `migrations/001_memory_schema.sql` is not run
   automatically.
2. **Retrieval is O(N) with a full node scan** despite the "O(1) query" framing; only *ingest* is
   approximately O(1). No ANN/dense retrieval exists in the facade.
3. **`recency` decays in seconds**, effectively pinning every node's recency to ~0. Better:
   exponential half-life normalized by the graph's time span.
4. **Dead config fields**: `embedding_enabled`, `embedding_model`, `prune_min_access`.
   `Node.embedding` is never populated by `CtxFlow.ingest`.
5. **`Node.status` is annotated `str`** but assigned `NodeStatus` members, so consumers defensively
   do `isinstance(x, str) else x.value`. Pick one representation.
6. **Edge overwrite semantics**: `add_edge` cannot hold two relations for the same ordered pair, and
   re-ingesting the same `related_to` pair silently replaces the weight.
7. **Prune "drop" has no edge healing** (the proposal/spec text mentions it), and "summarize"
   returns only the removed IDs, discarding the summary node IDs.
8. **Degree cap is direction-blind in practice** — only the outgoing side of the new node is
   checked, and explicit source edges also check the *source's* out-degree, so behaviour is uneven.
9. **Contradiction detection is off by default** and needs an `llm`; without one,
   `detect_and_apply` is a no-op. The eval harness sidesteps it by injecting supersession edges
   from the dataset, so the benchmark does not actually exercise the LLM detector.
10. **Router passes `query=` to every agent**, including agents that ignore it; `previous_results`
    plus `context["previous_output"]` are both consulted with slightly different shapes.
11. **`TraceAgent` is registered under the router key `"graph_query"`**, colliding with the skill
    name; `ReaderAgent` re-implements abstract sentence splitting that also exists in
    `routes/papers.py` and `PaperReaderSkill`.
12. **`paper_reader.read_pdf` returns `page_count = len(sections)`** before best-effort correction,
    so the fallback value is misleading.
13. **`LiteratureSearchSkill` swallows every network error** and returns `[]`; no retries, no
    backoff, no API-key support, no pagination. Its SQLite connection is owned per instance.
14. **Global mutable state in the API**: each route module holds a module-level `_app_state` dict;
    two apps in one process would collide. Not thread/task safe.
15. **`/api/stream` only emits coarse events** (start, one per completed agent, done) — it does not
    stream tokens, and it runs the whole pipeline in one executor call before emitting.
16. **UI is not using SSE**, and graph layout is hand-rolled (no d3/cytoscape), so it degrades on
    larger graphs.
17. **Chat has no memory/session handling** — `ChatRequest.session_id` and `token_budget` are
    accepted and ignored. `project_id` isolates notes only; papers/traces are global.
18. **Default `SkillRouter` is built with no `llm`**, so in the running API all synthesis is
    heuristic. There is no configuration surface for wiring an LLM (no env var, no factory arg
    besides injecting a pre-built `router_agent`).
19. **`ctxflow/__init__.py`, `researchos/__init__.py` docstrings contain mojibake** (mojibake
    artifacts from a cp1252 round-trip) — safe to rewrite.
20. **`tests/llm.py` executes network code at import** and needs `openai` + `NVIDIA_API_KEY`.
21. **Filenames with inconsistent casing**: `memory/inMemorystore.py` (not `in_memory_store.py`).

---

## 12. Implemented vs. specified-only (do not confuse the two)

`docs/proposed.md` and `CODEBASE_LAYERS_AND_OWNERSHIP.md` describe the v1 scope, most of which
exists. `ctxflow_to_researchos_roadmap.md` (v2) describes a much larger target that is **not**
implemented. Do not assume these exist:

- `TokenizerAdapter`-backed `BudgetPlanner` with per-model YAML profiles and hard post-compile
  token checks (`B_memory = B_context − system − tools − query − output_reserve − safety`).
- `QueryAnalyzer` with query classes (`fact_lookup`, `current_state`, `history_why`, `multi_hop`,
  `preference`, `procedural`, `general`) and a labelled evaluation set.
- Tiered, **prefix-stable** context layout (stable prefix for cache reuse; ordering experiments
  confined to the query-specific tier) and plan-level cache-cost accounting.
- Personalized PageRank retrieval, MMR redundancy elimination, trust-aware scoring,
  correction/cascading propagation, validity state machine beyond the three node statuses.
- Hybrid dense retrieval / embeddings in the facade, HNSW-backed graph search.
- MCP server, LangGraph/CrewAI adapters, skill marketplace, experiment manager, timeline.

What *is* implemented from the larger vision: tag index, degree governance, BFS proximity,
Jaccard+recency+proximity scoring, drop/summarize pruning, supersession edges with confidence and
evidence, backward provenance traversal, Research Trace (API + UI), four agents with deterministic
routing, five registered skills, arXiv/S2 caching, FastAPI + SSE, and a three-system eval harness.

---

## 13. Suggested build order for a from-scratch implementation

1. `models.py` (dataclasses, config + validation) → `graph.py` (NetworkX wrapper, tag index, BFS,
   provenance walk) → tests.
2. `governance.py` → `extractors.py` → `query.py` → `pruning.py` → `serialization.py` → tests.
3. `contradiction.py` + `llm_extractor.py` (LLM as an injected callable) → `CtxFlow` facade.
4. `researchos/skills/registry.py` (schema validation + invoke) → `graph_query.py` →
   `literature_search.py` (cache) → `paper_reader.py` (PyMuPDF) → `review_writer.py`.
5. `agents/base.py` → `router.py` → `retriever.py` → `reader.py` → `synthesizer.py` →
   `critic.py` → `trace.py`.
6. `api/schemas.py` → route modules → `server.py` (lifespan DI, SSE, static mount) → `main.py`.
7. `web/index.html` + `app.js` (graph render + trace panel + chat).
8. `eval/dataset.py` → `metrics.py` → `baselines.py` → `benchmark.py` → `__main__.py`.
9. Optional but high value: replace the in-memory-only persistence with the `ctxflow.memory`
   Postgres path, add a real LLM provider adapter, and wire SSE token streaming.

---

## 14. Copy-paste prompt for the receiving model

> You are implementing "CtxFlow + ResearchOS", a Python 3.9+ package (`src/ctxflow`,
> `src/researchos`) that provides a schema-agnostic graph memory engine for LLM agents plus a
> multi-agent research assistant on top of it.
>
> Required layers:
> 1. **Graph engine** — dataclass `Node`/`Edge` with open-vocabulary `node_type`/`relation_type`;
>    `ContextGraph` over `networkx.DiGraph` with an inverted tag index, O(1) insertion, undirected
>    BFS distances, Jaccard tag similarity, and backward provenance traversal over
>    `follows|derived_from|extracted_from|references`.
> 2. **Retrieval & governance** — score = α·Jaccard(tags) + β·recency + γ·1/(1+hops) with
>    α+β+γ=1 (defaults 0.5/0.3/0.2), optional `/(degree+1)` normalization, out-degree cap
>    (default 50), superseded nodes excluded by default.
> 3. **Truth maintenance** — candidate filtering by tag similarity, then an injected LLM callable
>    `(prompt, system) -> str` classifying each pair as `supersedes|contradicts|supports|unrelated`
>    with a confidence; apply an edge plus `status`/`superseded_by` transitions above a
>    confidence threshold; `research_trace(node_id)` returns the chain, superseded claims,
>    contradictions and source papers.
> 4. **Maintenance** — staleness = weighted age/access-frequency/last-access; `drop` or
>    `summarize` (connected stale clusters → summary node linked with `summarizes`).
> 5. **Persistence** — JSON and optional MessagePack.
> 6. **Skills** — a registry with `SkillSchema`/`SkillResult` and minimal JSON-Schema validation;
>    skills: graph query + research trace, arXiv/Semantic Scholar search with a SQLite TTL cache,
>    PDF section/claim extraction, and multi-paper review writing.
> 7. **Agents** — `BaseAgent` with timing/error capture; deterministic router classifying intent
>    into NEW_LITERATURE / EXISTING_KNOWLEDGE / CLAIM_VERIFICATION / EXPLORATORY_TRACE and running
>    the minimal plan (retriever→reader→synthesizer→critic, synthesizer→critic, critic,
>    trace). Synthesizer must be positional-bias aware (high-salience items at the edges of the
>    packed context) and token-budgeted. Critic must produce a VERIFIED / WARNINGS_FOUND verdict.
> 8. **API** — FastAPI with lifespan dependency injection, `/health`, `/api/chat`, `/api/query`,
>    `/api/search`, `/api/papers/ingest`, `/api/papers/upload`, `/api/graph/stats`,
>    `/api/graph/data`, `/api/trace`, `/api/notes`, `/api/skills`, an SSE `/api/stream`, and a
>    bundled static workspace UI at `/`.
> 9. **Evaluation** — a deterministic, LLM-free harness comparing graph memory + 4 agents against
>    (A) flat-vector single-agent and (B) flat-vector 4-agent on answer accuracy,
>    supersession-defence rate, provenance accuracy and tokens per query, over a superseded-claim
>    dataset, with a CLI (`researchos-eval`).
>
> Constraints: every LLM interaction goes through an injected callable and must degrade gracefully;
> no provider SDK imports in library code; the graph store is in-memory by default; keep the public
> names and metric definitions in §10 stable. Deliver tests for each layer (the reference has 120
> passing tests) and keep the UI dependency-free (no npm build).
