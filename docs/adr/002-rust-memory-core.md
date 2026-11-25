# ADR-002: Rust memory core with four graph layers

**Status:** PROPOSED (2026-11-25)

## Context

`ContextGraph` wraps `networkx.MultiDiGraph`. Every node and edge is a Python
dict/object graph, so memory grows at roughly 825 bytes per edge
(`scripts/bench_graph_memory.py`: 100k nodes, 800k edges, 660 MB). Mutation is
also GIL-bound, which makes a large shared in-memory graph impractical.

## Decision

- Rewrite only the CtxFlow memory core in Rust (`rust/ctxflow-core`). ResearchOS,
  LangGraph orchestration, LLM extraction and embedding models stay in Python.
- One node table, four edge layers. Each relation type belongs to exactly one
  layer; its semantic category (`provenance`, `contradiction`, `structural`,
  `research`) is kept separately so traversals keep the Python semantics.

  | Layer | Default relations | Purpose |
  |---|---|---|
  | temporal | `follows`, `supersedes`, `precedes` | ordering, supersession, validity |
  | provenance | `derived_from`, `extracted_from`, `references`, `cites`, `summary_of`, `summarizes`, `duplicate_of` | lineage and audit |
  | causal | `caused_by`, `causes`, `depends_on`, `based_on`, `supports`, `extends`, `contradicts` | confidence-weighted influence |
  | entity | `related_to`, `evaluated_on`, `mentions`, `is_a`, `part_of`, and any unregistered relation | entity relations |

  A relation's layer cannot change after registration.
- Compact storage: node keys interned to `u32`, relations to `u16`, edges as
  fixed 32-byte records. Each layer holds outgoing and incoming CSR bases plus a
  small mutable delta and tombstone set, which are folded in by compaction.
  Node content, tags and embeddings stay outside the hot graph.
- Valid time on nodes and edges (`valid_from`/`valid_to`, microseconds) plus a
  sorted creation-time index. Supersession closes the old node's interval and
  adds a temporal `supersedes` edge.
- Governance in the core: out-degree cap across all layers (replacing an existing
  `(src, dst, relation)` never counts) and a minimum confidence for causal edges
  proposed by non-`rule`/`user` origins (LLM, Laya).
- Concurrency: one writer thread owns the graph (actor). Commands arrive on a
  bounded mailbox, are applied in batches, written to a CRC-framed WAL and
  acknowledged after the batch commits. Each batch publishes an immutable
  `Arc<Graph>` snapshot; readers never block the writer. A WAL write failure
  stops the writer (fail-stop) rather than acknowledging non-durable writes.
- The core has no third-party dependencies.

## Consequences

- Expected footprint is about 10x smaller than networkx (two 32-byte records
  per edge plus about 56 bytes per node and its interned key). Confirm with
  `cargo run --release --example mem_bench` against `scripts/bench_graph_memory.py`.
- Snapshots share structure with the live graph. Node records and edge deltas
  are copy-on-write chunks (1024 nodes/owners each), so the first write after
  a publish copies only the chunks it touches. Compaction (`compact_threshold`)
  rebuilds the CSR in O(edges) and currently runs inline on the writer thread.
- Not yet included: PyO3 bindings, Postgres write-through, tag/Jaccard and
  vector retrieval, access statistics and pruning. The Python test suite is the
  intended conformance suite once bindings exist.
- Bitemporal (transaction-time) queries are not supported; the WAL records
  commit order only.
