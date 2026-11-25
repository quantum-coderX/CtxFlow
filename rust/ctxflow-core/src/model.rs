//! Compact value types for nodes, edges and the four graph layers.

use std::time::{SystemTime, UNIX_EPOCH};

/// Microseconds since the Unix epoch.
pub type Micros = i64;

/// `valid_to` value for an interval that has not ended.
pub const OPEN_END: Micros = i64::MAX;

pub fn now_micros() -> Micros {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as Micros)
        .unwrap_or(0)
}

/// Dense node handle. Also the index into the node table.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
#[repr(transparent)]
pub struct NodeId(pub u32);

/// Interned relation-type handle.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
#[repr(transparent)]
pub struct RelId(pub u16);

/// Storage partition for edges. Each relation type lives in exactly one layer.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Layer {
    Temporal = 0,
    Provenance = 1,
    Causal = 2,
    Entity = 3,
}

impl Layer {
    pub const ALL: [Layer; 4] = [Layer::Temporal, Layer::Provenance, Layer::Causal, Layer::Entity];

    pub fn index(self) -> usize {
        self as usize
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Layer::Temporal => "temporal",
            Layer::Provenance => "provenance",
            Layer::Causal => "causal",
            Layer::Entity => "entity",
        }
    }

    pub fn parse(s: &str) -> Option<Layer> {
        Layer::ALL.into_iter().find(|l| l.as_str() == s)
    }

    pub(crate) fn from_u8(v: u8) -> Option<Layer> {
        Layer::ALL.get(v as usize).copied()
    }
}

/// Semantic category, mirroring `ctxflow.vocabulary.RelationCategory`.
/// Traversals ask for a category; storage is decided by [`Layer`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Category {
    Provenance = 0,
    Contradiction = 1,
    Structural = 2,
    Research = 3,
}

impl Category {
    pub const ALL: [Category; 4] = [
        Category::Provenance,
        Category::Contradiction,
        Category::Structural,
        Category::Research,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Category::Provenance => "provenance",
            Category::Contradiction => "contradiction",
            Category::Structural => "structural",
            Category::Research => "research",
        }
    }

    pub fn parse(s: &str) -> Option<Category> {
        Category::ALL.into_iter().find(|c| c.as_str() == s)
    }

    pub(crate) fn from_u8(v: u8) -> Option<Category> {
        Category::ALL.get(v as usize).copied()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum NodeStatus {
    Active = 0,
    Superseded = 1,
    Contested = 2,
    /// Tombstone. Ids are never reused, so removed nodes keep their slot.
    Deleted = 3,
}

impl NodeStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            NodeStatus::Active => "active",
            NodeStatus::Superseded => "superseded",
            NodeStatus::Contested => "contested",
            NodeStatus::Deleted => "deleted",
        }
    }
}

/// Per-node hot state. Content, summaries, tags and embeddings live outside
/// the core (Postgres / Python); only what traversal and scoring need is here.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct NodeRecord {
    pub created_at: Micros,
    pub valid_from: Micros,
    pub valid_to: Micros,
    pub superseded_by: Option<NodeId>,
    pub importance: f32,
    pub node_type: u16,
    pub status: NodeStatus,
}

impl NodeRecord {
    pub(crate) fn vacant() -> Self {
        NodeRecord {
            created_at: 0,
            valid_from: 0,
            valid_to: 0,
            superseded_by: None,
            importance: 0.0,
            node_type: 0,
            status: NodeStatus::Deleted,
        }
    }

    pub fn is_live(&self) -> bool {
        self.status != NodeStatus::Deleted
    }

    /// Valid-time check: live and `valid_from <= t < valid_to`.
    pub fn valid_at(&self, t: Micros) -> bool {
        self.is_live() && self.valid_from <= t && t < self.valid_to
    }
}

/// One adjacency entry. `other` is the target for outgoing lists and the
/// source for incoming lists. Fixed at 32 bytes.
#[derive(Clone, Copy, Debug, PartialEq)]
#[repr(C)]
pub struct EdgeRec {
    pub other: u32,
    pub rel: RelId,
    pub origin: u16,
    pub weight: f32,
    pub confidence: f32,
    pub valid_from: Micros,
    pub valid_to: Micros,
}

const _: () = assert!(std::mem::size_of::<EdgeRec>() == 32);

impl EdgeRec {
    pub fn valid_at(&self, t: Micros) -> bool {
        self.valid_from <= t && t < self.valid_to
    }
}

/// A resolved directed edge returned by queries.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EdgeView {
    pub src: NodeId,
    pub dst: NodeId,
    pub layer: Layer,
    pub relation: RelId,
    pub origin: u16,
    pub weight: f32,
    pub confidence: f32,
    pub valid_from: Micros,
    pub valid_to: Micros,
}

impl EdgeView {
    pub(crate) fn outgoing(owner: u32, rec: &EdgeRec, layer: Layer) -> Self {
        Self::build(NodeId(owner), NodeId(rec.other), rec, layer)
    }

    pub(crate) fn incoming(owner: u32, rec: &EdgeRec, layer: Layer) -> Self {
        Self::build(NodeId(rec.other), NodeId(owner), rec, layer)
    }

    fn build(src: NodeId, dst: NodeId, rec: &EdgeRec, layer: Layer) -> Self {
        EdgeView {
            src,
            dst,
            layer,
            relation: rec.rel,
            origin: rec.origin,
            weight: rec.weight,
            confidence: rec.confidence,
            valid_from: rec.valid_from,
            valid_to: rec.valid_to,
        }
    }

    pub fn valid_at(&self, t: Micros) -> bool {
        self.valid_from <= t && t < self.valid_to
    }
}

/// Node upsert request. Timestamps are explicit so WAL replay is deterministic.
#[derive(Clone, Debug, PartialEq)]
pub struct NodeInput {
    pub key: String,
    pub node_type: String,
    pub created_at: Micros,
    pub valid_from: Micros,
    pub valid_to: Micros,
    pub importance: f32,
}

impl NodeInput {
    pub fn new(key: impl Into<String>) -> Self {
        let now = now_micros();
        NodeInput {
            key: key.into(),
            node_type: "generic".to_string(),
            created_at: now,
            valid_from: now,
            valid_to: OPEN_END,
            importance: 0.5,
        }
    }

    pub fn with_type(mut self, node_type: impl Into<String>) -> Self {
        self.node_type = node_type.into();
        self
    }

    /// Sets both creation time and start of validity.
    pub fn at(mut self, t: Micros) -> Self {
        self.created_at = t;
        self.valid_from = t;
        self
    }

    pub fn valid_until(mut self, t: Micros) -> Self {
        self.valid_to = t;
        self
    }

    pub fn with_importance(mut self, importance: f32) -> Self {
        self.importance = importance;
        self
    }
}

/// Edge upsert request. The `(src, dst, relation)` triple is the identity:
/// upserting an existing triple replaces it.
#[derive(Clone, Debug, PartialEq)]
pub struct EdgeInput {
    pub src: String,
    pub dst: String,
    pub relation: String,
    pub weight: f32,
    pub confidence: f32,
    pub origin: String,
    pub valid_from: Micros,
    pub valid_to: Micros,
}

impl EdgeInput {
    pub fn new(src: impl Into<String>, dst: impl Into<String>, relation: impl Into<String>) -> Self {
        EdgeInput {
            src: src.into(),
            dst: dst.into(),
            relation: relation.into(),
            weight: 1.0,
            confidence: 1.0,
            origin: "rule".to_string(),
            valid_from: now_micros(),
            valid_to: OPEN_END,
        }
    }

    pub fn with_weight(mut self, weight: f32) -> Self {
        self.weight = weight;
        self
    }

    pub fn with_confidence(mut self, confidence: f32) -> Self {
        self.confidence = confidence;
        self
    }

    pub fn with_origin(mut self, origin: impl Into<String>) -> Self {
        self.origin = origin.into();
        self
    }

    pub fn valid(mut self, from: Micros, to: Micros) -> Self {
        self.valid_from = from;
        self.valid_to = to;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProvenanceStep {
    pub node: NodeId,
    /// Edge linking this node to the previous step (`None` for the root).
    pub edge: Option<EdgeView>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProvenanceChain {
    pub root: NodeId,
    pub steps: Vec<ProvenanceStep>,
    pub contradictions: Vec<(NodeId, EdgeView)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Out,
    In,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CausalHop {
    pub node: NodeId,
    pub depth: u32,
    /// Product of edge confidences along the best path found.
    pub confidence: f32,
}
