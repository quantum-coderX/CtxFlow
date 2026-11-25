//! CtxFlow memory core: a compact four-layer graph with a single-writer actor.
//!
//! * [`Graph`]: typed multigraph split into temporal, provenance, causal and
//!   entity layers that share one node table. Edges are 32-byte records in
//!   per-layer CSR bases with small mutable deltas (see `adjacency`).
//! * [`MemoryEngine`]: owns the graph on a writer thread, batches commands,
//!   logs them to a [`wal::Wal`], and publishes immutable snapshots that any
//!   number of reader threads can query concurrently.

mod adjacency;
mod chunked;
pub mod engine;
mod error;
mod graph;
mod model;
mod time_index;
pub mod vocab;
pub mod wal;

pub use engine::{Command, EngineConfig, EngineHandle, MemoryEngine, Reply};
pub use error::{Error, Result};
pub use graph::{Graph, GraphConfig};
pub use model::{
    now_micros, CausalHop, Category, Direction, EdgeInput, EdgeRec, EdgeView, Layer, Micros,
    NodeId, NodeInput, NodeRecord, NodeStatus, ProvenanceChain, ProvenanceStep, RelId, OPEN_END,
};
pub use vocab::{RelationSpec, Vocabulary};
