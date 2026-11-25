//! The four-layer memory graph.
//!
//! `Graph` is a plain single-threaded value. Cloning is cheap (copy-on-write
//! chunks and Arc-shared CSR bases), which is what the engine uses to publish
//! immutable read snapshots after each write batch.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};
use std::sync::{Arc, RwLock, RwLockReadGuard, RwLockWriteGuard};

use crate::adjacency::Adjacency;
use crate::chunked::ChunkedVec;
use crate::error::{Error, Result};
use crate::model::{
    CausalHop, Category, Direction, EdgeInput, EdgeRec, EdgeView, Layer, Micros, NodeId,
    NodeInput, NodeRecord, NodeStatus, ProvenanceChain, ProvenanceStep, RelId, OPEN_END,
};
use crate::time_index::TimeIndex;
use crate::vocab::{RelationSpec, Symbols, DEFAULT_LAYER};

#[derive(Clone, Debug)]
pub struct GraphConfig {
    /// Maximum outgoing edges per node across all layers. Replacing an
    /// existing `(src, dst, relation)` triple never counts against the cap.
    pub degree_cap: Option<u32>,
    /// Causal-layer edges whose origin is not `rule` or `user` (i.e. proposed
    /// by an LLM or Laya) are rejected below this confidence.
    pub min_inferred_causal_confidence: f32,
    /// Pending delta entries that trigger a compaction in the engine.
    pub compact_threshold: usize,
}

impl Default for GraphConfig {
    fn default() -> Self {
        GraphConfig {
            degree_cap: Some(50),
            min_inferred_causal_confidence: 0.5,
            compact_threshold: 64 * 1024,
        }
    }
}

#[derive(Clone, Debug, Default)]
struct LayerStore {
    out: Adjacency,
    inc: Adjacency,
    edges: usize,
}

impl LayerStore {
    fn upsert(&mut self, src: u32, dst: u32, rec: EdgeRec) -> bool {
        let replaced = self.out.upsert(src, EdgeRec { other: dst, ..rec });
        self.inc.upsert(dst, EdgeRec { other: src, ..rec });
        if !replaced {
            self.edges += 1;
        }
        replaced
    }

    fn remove(&mut self, src: u32, dst: u32, rel: Option<RelId>) -> usize {
        let removed = self.out.remove(src, dst, rel);
        for e in &removed {
            self.inc.remove(dst, src, Some(e.rel));
        }
        self.edges -= removed.len();
        removed.len()
    }
}

#[derive(Clone, Debug)]
pub struct Graph {
    config: GraphConfig,
    symbols: Arc<RwLock<Symbols>>,
    nodes: ChunkedVec<NodeRecord>,
    live_nodes: usize,
    layers: [LayerStore; 4],
    time: TimeIndex,
}

impl Default for Graph {
    fn default() -> Self {
        Graph::new(GraphConfig::default())
    }
}

impl Graph {
    pub fn new(config: GraphConfig) -> Self {
        Graph {
            config,
            symbols: Arc::new(RwLock::new(Symbols::default())),
            nodes: ChunkedVec::default(),
            live_nodes: 0,
            layers: Default::default(),
            time: TimeIndex::default(),
        }
    }

    pub fn config(&self) -> &GraphConfig {
        &self.config
    }

    fn sym(&self) -> RwLockReadGuard<'_, Symbols> {
        self.symbols.read().unwrap_or_else(|e| e.into_inner())
    }

    fn sym_mut(&self) -> RwLockWriteGuard<'_, Symbols> {
        self.symbols.write().unwrap_or_else(|e| e.into_inner())
    }

    // -- Symbols ------------------------------------------------------------

    pub fn register_relation(
        &mut self,
        name: &str,
        layer: Layer,
        category: Option<Category>,
        description: &str,
    ) -> Result<RelId> {
        self.sym_mut().vocab.register(name, layer, category, description)
    }

    pub fn relation_id(&self, name: &str) -> Option<RelId> {
        self.sym().vocab.resolve(name)
    }

    pub fn relation(&self, id: RelId) -> Option<RelationSpec> {
        self.sym().vocab.spec(id).cloned()
    }

    pub fn relation_name(&self, id: RelId) -> Option<String> {
        self.relation(id).map(|s| s.name)
    }

    pub fn relations_in(&self, category: Category) -> Vec<RelId> {
        self.sym().vocab.relations_in(category)
    }

    pub fn origin_name(&self, origin: u16) -> Option<String> {
        self.sym().origins.name(origin).map(str::to_string)
    }

    pub fn node_type_name(&self, node_type: u16) -> Option<String> {
        self.sym().node_types.name(node_type).map(str::to_string)
    }

    /// Resolves a live node key to its id.
    pub fn id(&self, key: &str) -> Option<NodeId> {
        let id = self.sym().lookup_key(key)?;
        self.live(id).then_some(id)
    }

    pub fn key(&self, id: NodeId) -> Option<Arc<str>> {
        if !self.live(id) {
            return None;
        }
        self.sym().key(id)
    }

    fn live(&self, id: NodeId) -> bool {
        self.nodes.get(id.0 as usize).map_or(false, NodeRecord::is_live)
    }

    fn node_valid_at(&self, id: NodeId, t: Micros) -> bool {
        self.nodes.get(id.0 as usize).map_or(false, |n| n.valid_at(t))
    }

    // -- Nodes --------------------------------------------------------------

    /// Inserts a node or updates an existing one. Updates keep the original
    /// creation time and supersession state.
    pub fn upsert_node(&mut self, input: &NodeInput) -> Result<NodeId> {
        if input.key.is_empty() {
            return Err(Error::InvalidInput("node key must be non-empty".into()));
        }
        if input.valid_to < input.valid_from {
            return Err(Error::InvalidInput(format!(
                "node {:?} has valid_to before valid_from",
                input.key
            )));
        }
        let (id, node_type) = {
            let mut sym = self.sym_mut();
            let id = sym.intern_key(&input.key)?;
            (id, sym.node_types.intern(&input.node_type, "node type")?)
        };
        let index = id.0 as usize;
        self.nodes.ensure_len(index + 1, NodeRecord::vacant());
        let slot = self.nodes.get_mut(index).expect("slot was just ensured");
        let prev = *slot;
        let revived = !prev.is_live();
        *slot = NodeRecord {
            created_at: if revived { input.created_at } else { prev.created_at },
            valid_from: input.valid_from,
            valid_to: input.valid_to,
            superseded_by: if revived { None } else { prev.superseded_by },
            importance: input.importance,
            node_type,
            status: if revived { NodeStatus::Active } else { prev.status },
        };
        if revived {
            self.live_nodes += 1;
            self.time.insert(input.created_at, id.0);
        }
        Ok(id)
    }

    /// Tombstones a node and drops its incident edges in every layer.
    pub fn remove_node(&mut self, key: &str) -> bool {
        let Some(id) = self.id(key) else {
            return false;
        };
        for store in &mut self.layers {
            let outgoing: Vec<EdgeRec> = store.out.iter(id.0).collect();
            for e in outgoing {
                store.remove(id.0, e.other, Some(e.rel));
            }
            let incoming: Vec<EdgeRec> = store.inc.iter(id.0).collect();
            for e in incoming {
                store.remove(e.other, id.0, Some(e.rel));
            }
        }
        if let Some(slot) = self.nodes.get_mut(id.0 as usize) {
            slot.status = NodeStatus::Deleted;
        }
        self.live_nodes -= 1;
        true
    }

    pub fn node(&self, key: &str) -> Option<NodeRecord> {
        self.id(key).and_then(|id| self.node_by_id(id))
    }

    pub fn node_by_id(&self, id: NodeId) -> Option<NodeRecord> {
        self.nodes.get(id.0 as usize).copied().filter(NodeRecord::is_live)
    }

    pub fn has_node(&self, key: &str) -> bool {
        self.id(key).is_some()
    }

    pub fn nodes_by_status(&self, status: NodeStatus) -> Vec<NodeId> {
        self.nodes
            .iter()
            .enumerate()
            .filter(|(_, n)| n.status == status)
            .map(|(i, _)| NodeId(i as u32))
            .collect()
    }

    /// Marks `old` superseded by `new` at time `at`: closes `old`'s validity
    /// interval and records a `new -supersedes-> old` temporal edge.
    pub fn supersede(&mut self, old_key: &str, new_key: &str, at: Micros) -> Result<()> {
        let old = self.id(old_key).ok_or_else(|| Error::UnknownNode(old_key.to_string()))?;
        let new = self.id(new_key).ok_or_else(|| Error::UnknownNode(new_key.to_string()))?;
        if old == new {
            return Err(Error::InvalidInput("a node cannot supersede itself".into()));
        }
        let edge = EdgeInput {
            src: new_key.to_string(),
            dst: old_key.to_string(),
            relation: "supersedes".to_string(),
            weight: 1.0,
            confidence: 1.0,
            origin: "rule".to_string(),
            valid_from: at,
            valid_to: OPEN_END,
        };
        self.put_edge(&edge, false)?;
        let slot = self.nodes.get_mut(old.0 as usize).expect("live node has a slot");
        slot.status = NodeStatus::Superseded;
        slot.superseded_by = Some(new);
        slot.valid_to = slot.valid_to.min(at.max(slot.valid_from));
        Ok(())
    }

    // -- Edges --------------------------------------------------------------

    /// Inserts or replaces an edge, enforcing governance. Returns `true` when
    /// an existing `(src, dst, relation)` edge was replaced.
    pub fn upsert_edge(&mut self, input: &EdgeInput) -> Result<bool> {
        self.put_edge(input, true)
    }

    /// `enforce = false` is used for WAL replay and internal edges: those
    /// mutations were already accepted once and must replay identically.
    pub(crate) fn put_edge(&mut self, input: &EdgeInput, enforce: bool) -> Result<bool> {
        if !input.weight.is_finite() || !(0.0..=1.0).contains(&input.confidence) {
            return Err(Error::InvalidInput(
                "edge weight must be finite and confidence in [0, 1]".into(),
            ));
        }
        if input.valid_to < input.valid_from {
            return Err(Error::InvalidInput("edge valid_to is before valid_from".into()));
        }
        let src = self.id(&input.src).ok_or_else(|| Error::UnknownNode(input.src.clone()))?;
        let dst = self.id(&input.dst).ok_or_else(|| Error::UnknownNode(input.dst.clone()))?;
        // Resolve read-only first: a rejected edge must not register symbols,
        // because rejected commands are not logged and would not replay.
        let (known_rel, layer) = {
            let sym = self.sym();
            match sym.vocab.resolve(&input.relation) {
                Some(rel) => (Some(rel), sym.vocab.spec(rel).expect("resolved relation has a spec").layer),
                None => (None, DEFAULT_LAYER),
            }
        };

        if enforce {
            let exists = known_rel
                .map_or(false, |rel| self.layers[layer.index()].out.find(src.0, dst.0, rel).is_some());
            if !exists {
                if let Some(cap) = self.config.degree_cap {
                    if self.out_degree_id(src) >= cap as usize {
                        return Err(Error::DegreeCapExceeded { node: input.src.clone(), cap });
                    }
                }
            }
            let trusted = matches!(input.origin.as_str(), "rule" | "user");
            let required = self.config.min_inferred_causal_confidence;
            if layer == Layer::Causal && !trusted && input.confidence < required {
                return Err(Error::ConfidenceTooLow {
                    relation: input.relation.clone(),
                    confidence: input.confidence,
                    required,
                });
            }
        }

        let (rel, origin) = {
            let mut sym = self.sym_mut();
            let origin = sym.origins.intern(&input.origin, "origin")?;
            let rel = match known_rel {
                Some(rel) => rel,
                None => sym.vocab.resolve_or_register(&input.relation)?,
            };
            (rel, origin)
        };

        let rec = EdgeRec {
            other: dst.0,
            rel,
            origin,
            weight: input.weight,
            confidence: input.confidence,
            valid_from: input.valid_from,
            valid_to: input.valid_to,
        };
        Ok(self.layers[layer.index()].upsert(src.0, dst.0, rec))
    }

    /// Removes one relation between two nodes, or all of them when
    /// `relation` is `None`. Returns the number of edges removed.
    pub fn remove_edge(&mut self, src: &str, dst: &str, relation: Option<&str>) -> usize {
        let (Some(s), Some(d)) = (self.id(src), self.id(dst)) else {
            return 0;
        };
        match relation {
            Some(name) => match self.relation_id(name) {
                Some(rel) => {
                    let layer = self.layer_of(rel);
                    self.layers[layer.index()].remove(s.0, d.0, Some(rel))
                }
                None => 0,
            },
            None => self.layers.iter_mut().map(|store| store.remove(s.0, d.0, None)).sum(),
        }
    }

    fn layer_of(&self, rel: RelId) -> Layer {
        self.sym().vocab.spec(rel).map_or(DEFAULT_LAYER, |s| s.layer)
    }

    pub fn has_edge(&self, src: &str, dst: &str, relation: Option<&str>) -> bool {
        let (Some(s), Some(d)) = (self.id(src), self.id(dst)) else {
            return false;
        };
        match relation {
            Some(name) => match self.relation_id(name) {
                Some(rel) => self.layers[self.layer_of(rel).index()].out.find(s.0, d.0, rel).is_some(),
                None => false,
            },
            None => self.layers.iter().any(|store| store.out.iter(s.0).any(|e| e.other == d.0)),
        }
    }

    pub fn edges_between(&self, src: &str, dst: &str) -> Vec<EdgeView> {
        let (Some(s), Some(d)) = (self.id(src), self.id(dst)) else {
            return Vec::new();
        };
        self.collect_edges(s, Direction::Out, None)
            .into_iter()
            .filter(|e| e.dst == d)
            .collect()
    }

    pub fn out_edges(&self, key: &str, layer: Option<Layer>) -> Vec<EdgeView> {
        self.id(key).map_or_else(Vec::new, |id| self.collect_edges(id, Direction::Out, layer))
    }

    pub fn in_edges(&self, key: &str, layer: Option<Layer>) -> Vec<EdgeView> {
        self.id(key).map_or_else(Vec::new, |id| self.collect_edges(id, Direction::In, layer))
    }

    /// Edges whose own validity and whose far endpoint are both valid at `t`.
    pub fn edges_as_of(&self, key: &str, direction: Direction, layer: Option<Layer>, t: Micros) -> Vec<EdgeView> {
        let Some(id) = self.id(key) else {
            return Vec::new();
        };
        self.collect_edges(id, direction, layer)
            .into_iter()
            .filter(|e| {
                let far = if direction == Direction::Out { e.dst } else { e.src };
                e.valid_at(t) && self.node_valid_at(far, t)
            })
            .collect()
    }

    fn collect_edges(&self, id: NodeId, direction: Direction, layer: Option<Layer>) -> Vec<EdgeView> {
        let mut edges = Vec::new();
        for l in Layer::ALL {
            if layer.map_or(false, |only| only != l) {
                continue;
            }
            let store = &self.layers[l.index()];
            match direction {
                Direction::Out => edges.extend(store.out.iter(id.0).map(|r| EdgeView::outgoing(id.0, &r, l))),
                Direction::In => edges.extend(store.inc.iter(id.0).map(|r| EdgeView::incoming(id.0, &r, l))),
            }
        }
        edges
    }

    fn out_degree_id(&self, id: NodeId) -> usize {
        self.layers.iter().map(|s| s.out.degree(id.0)).sum()
    }

    pub fn out_degree(&self, key: &str) -> usize {
        self.id(key).map_or(0, |id| self.out_degree_id(id))
    }

    pub fn in_degree(&self, key: &str) -> usize {
        self.id(key).map_or(0, |id| self.layers.iter().map(|s| s.inc.degree(id.0)).sum())
    }

    // -- Traversal ----------------------------------------------------------

    /// Bounded undirected BFS over the given layers (all layers if empty).
    /// Returns hop distances, excluding the start node.
    pub fn bfs_distances(&self, key: &str, max_depth: u32, layers: &[Layer]) -> HashMap<NodeId, u32> {
        let mut dist = HashMap::new();
        let Some(start) = self.id(key) else {
            return dist;
        };
        let layers: &[Layer] = if layers.is_empty() { &Layer::ALL } else { layers };
        let mut seen = HashSet::from([start.0]);
        let mut queue = VecDeque::from([(start.0, 0u32)]);
        while let Some((cur, depth)) = queue.pop_front() {
            if depth > 0 {
                dist.insert(NodeId(cur), depth);
            }
            if depth >= max_depth {
                continue;
            }
            for l in layers {
                let store = &self.layers[l.index()];
                for e in store.out.iter(cur).chain(store.inc.iter(cur)) {
                    if seen.insert(e.other) {
                        queue.push_back((e.other, depth + 1));
                    }
                }
            }
        }
        dist
    }

    /// Backward walk over provenance-category edges (any layer), collecting
    /// contradiction-category edges that touch the chain. Mirrors
    /// `ContextGraph.trace_provenance` in the Python package.
    pub fn trace_provenance(&self, key: &str, max_depth: u32) -> Option<ProvenanceChain> {
        let root = self.id(key)?;
        let provenance = self.relations_in(Category::Provenance);
        let contradiction = self.relations_in(Category::Contradiction);

        let mut steps = vec![ProvenanceStep { node: root, edge: None }];
        let mut contradictions = Vec::new();
        let mut visited = HashSet::from([root.0]);
        let mut queue = VecDeque::from([(root.0, 0u32)]);

        while let Some((cur, depth)) = queue.pop_front() {
            if depth >= max_depth {
                continue;
            }
            for l in Layer::ALL {
                let store = &self.layers[l.index()];
                for e in store.inc.iter(cur) {
                    if provenance.contains(&e.rel) && visited.insert(e.other) && self.live(NodeId(e.other)) {
                        steps.push(ProvenanceStep {
                            node: NodeId(e.other),
                            edge: Some(EdgeView::incoming(cur, &e, l)),
                        });
                        queue.push_back((e.other, depth + 1));
                    }
                }
            }
            for l in Layer::ALL {
                let store = &self.layers[l.index()];
                for e in store.inc.iter(cur).filter(|e| contradiction.contains(&e.rel)) {
                    contradictions.push((NodeId(e.other), EdgeView::incoming(cur, &e, l)));
                }
                for e in store.out.iter(cur).filter(|e| contradiction.contains(&e.rel)) {
                    contradictions.push((NodeId(e.other), EdgeView::outgoing(cur, &e, l)));
                }
            }
        }
        Some(ProvenanceChain { root, steps, contradictions })
    }

    /// Best-confidence reachability over the causal layer. Path confidence
    /// is the product of edge confidences; contradiction edges are skipped.
    /// With `as_of`, only edges and nodes valid at that time are followed.
    pub fn causal_walk(
        &self,
        key: &str,
        direction: Direction,
        max_depth: u32,
        min_confidence: f32,
        as_of: Option<Micros>,
    ) -> Vec<CausalHop> {
        let Some(start) = self.id(key) else {
            return Vec::new();
        };
        let contradiction = self.relations_in(Category::Contradiction);
        let store = &self.layers[Layer::Causal.index()];
        let adjacency = match direction {
            Direction::Out => &store.out,
            Direction::In => &store.inc,
        };

        let mut hops = Vec::new();
        let mut done = HashSet::new();
        let mut heap = BinaryHeap::from([Candidate { confidence: 1.0, depth: 0, node: start.0 }]);
        while let Some(Candidate { confidence, depth, node }) = heap.pop() {
            if !done.insert(node) {
                continue;
            }
            if node != start.0 {
                hops.push(CausalHop { node: NodeId(node), depth, confidence });
            }
            if depth >= max_depth {
                continue;
            }
            for e in adjacency.iter(node) {
                if contradiction.contains(&e.rel) || done.contains(&e.other) {
                    continue;
                }
                let reachable = match as_of {
                    Some(t) => e.valid_at(t) && self.node_valid_at(NodeId(e.other), t),
                    None => self.live(NodeId(e.other)),
                };
                let next = confidence * e.confidence;
                if reachable && next >= min_confidence {
                    heap.push(Candidate { confidence: next, depth: depth + 1, node: e.other });
                }
            }
        }
        hops
    }

    // -- Temporal -----------------------------------------------------------

    /// Live nodes created in `[from, to)`, ordered by creation time.
    pub fn nodes_created_between(&self, from: Micros, to: Micros) -> Vec<NodeId> {
        let mut found: Vec<(Micros, u32)> = self
            .time
            .range(from, to)
            .into_iter()
            .filter_map(|id| {
                let n = self.nodes.get(id as usize)?;
                (n.is_live() && from <= n.created_at && n.created_at < to).then_some((n.created_at, id))
            })
            .collect();
        found.sort_unstable();
        found.dedup();
        found.into_iter().map(|(_, id)| NodeId(id)).collect()
    }

    /// Nodes whose validity interval contains `t`.
    pub fn nodes_valid_at(&self, t: Micros) -> Vec<NodeId> {
        self.nodes
            .iter()
            .enumerate()
            .filter(|(_, n)| n.valid_at(t))
            .map(|(i, _)| NodeId(i as u32))
            .collect()
    }

    // -- Maintenance & stats ------------------------------------------------

    pub fn node_count(&self) -> usize {
        self.live_nodes
    }

    pub fn edge_count(&self) -> usize {
        self.layers.iter().map(|s| s.edges).sum()
    }

    pub fn layer_edge_count(&self, layer: Layer) -> usize {
        self.layers[layer.index()].edges
    }

    /// Entries waiting in deltas/tombstones; drives compaction.
    pub fn pending_delta(&self) -> usize {
        self.layers.iter().map(|s| s.out.pending() + s.inc.pending()).sum::<usize>() + self.time.pending()
    }

    /// Folds every delta into its CSR base.
    pub fn compact(&mut self) {
        let rows = self.nodes.len();
        for store in &mut self.layers {
            store.out.compact(rows);
            store.inc.compact(rows);
        }
        self.time.compact();
    }

    /// Approximate heap usage of this graph's own structures (shared symbol
    /// table included). Use a counting allocator for exact numbers.
    pub fn heap_bytes(&self) -> usize {
        let layers: usize = self.layers.iter().map(|s| s.out.heap_bytes() + s.inc.heap_bytes()).sum();
        self.nodes.heap_bytes() + layers + self.time.heap_bytes() + self.sym().heap_bytes()
    }
}

#[derive(Clone, Copy, Debug)]
struct Candidate {
    confidence: f32,
    depth: u32,
    node: u32,
}

impl Ord for Candidate {
    fn cmp(&self, other: &Self) -> Ordering {
        self.confidence
            .total_cmp(&other.confidence)
            .then_with(|| other.depth.cmp(&self.depth))
            .then_with(|| other.node.cmp(&self.node))
    }
}

impl PartialOrd for Candidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for Candidate {}
