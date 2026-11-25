# ctxflow-core

Rust memory core for CtxFlow: a compact four-layer graph (temporal, provenance,
causal, entity) behind a single-writer actor with snapshot readers and a WAL.
Design and trade-offs: [ADR-002](../../docs/adr/002-rust-memory-core.md).

```text
rust/
  Cargo.toml                 workspace
  ctxflow-core/
    src/model.rs             ids, layers, 32-byte EdgeRec, inputs and views
    src/vocab.rs             relation registry (layer + category), interning
    src/adjacency.rs         CSR base + delta + tombstones per direction
    src/chunked.rs           copy-on-write node table
    src/time_index.rs        creation-time index
    src/graph.rs             Graph: governance, queries, traversals, compaction
    src/wal.rs               CRC-framed write-ahead log
    src/engine.rs            MemoryEngine actor, EngineHandle, snapshots
    tests/                   graph parity tests, engine/WAL tests
    examples/mem_bench.rs    exact heap + latency benchmark
```

## Build and test

```powershell
cd rust
cargo test
cargo run --release --example mem_bench -- 100000 8
```

Baseline for comparison (from the repo root):

```powershell
$env:PYTHONPATH = "src"; python scripts/bench_graph_memory.py --nodes 100000 --degree 8
```

## Usage

```rust
use ctxflow_core::{Direction, EdgeInput, EngineConfig, MemoryEngine, NodeInput};

let engine = MemoryEngine::start(EngineConfig {
    wal_path: Some("memory.wal".into()),
    ..EngineConfig::default()
})?;
let h = engine.handle(); // Clone + Send; share across threads

h.upsert_node(NodeInput::new("paper-1").with_type("paper"))?;
h.upsert_node(NodeInput::new("claim-1").with_type("claim"))?;
h.upsert_edge(EdgeInput::new("claim-1", "paper-1", "derived_from"))?;

let snap = h.snapshot(); // immutable Arc<Graph>
let chain = snap.trace_provenance("claim-1", 10);
let effects = snap.causal_walk("claim-1", Direction::Out, 3, 0.5, None);
engine.shutdown()?;
```
