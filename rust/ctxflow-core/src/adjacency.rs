//! LSM-style adjacency: an immutable CSR base, a small mutable delta, and
//! tombstones for base entries that were removed or replaced.
//!
//! The delta is split into copy-on-write chunks of `1 << DELTA_BITS` owners,
//! so the first write after a snapshot copies one chunk, not the whole delta.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use crate::model::{EdgeRec, RelId};

/// Compressed sparse rows. `offsets[n]..offsets[n + 1]` indexes `edges` for
/// owner `n`; each row is sorted by `(other, rel)`.
#[derive(Debug, Default)]
pub(crate) struct Csr {
    offsets: Vec<u32>,
    edges: Vec<EdgeRec>,
}

impl Csr {
    fn row(&self, owner: u32) -> &[EdgeRec] {
        let i = owner as usize;
        if i + 1 >= self.offsets.len() {
            return &[];
        }
        &self.edges[self.offsets[i] as usize..self.offsets[i + 1] as usize]
    }
}

type EdgeKey = (u32, u32, u16);

const DELTA_BITS: u32 = 10;

#[derive(Clone, Debug, Default)]
struct DeltaChunk {
    rows: HashMap<u32, Vec<EdgeRec>>,
    /// Base entries hidden because they were removed or replaced.
    dead: HashSet<EdgeKey>,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct Adjacency {
    base: Arc<Csr>,
    chunks: Vec<Arc<DeltaChunk>>,
    delta_len: usize,
    dead_len: usize,
}

fn matches(e: &EdgeRec, other: u32, rel: Option<RelId>) -> bool {
    e.other == other && rel.map_or(true, |r| e.rel == r)
}

fn base_live(chunk: Option<&DeltaChunk>, owner: u32, e: &EdgeRec) -> bool {
    chunk.map_or(true, |c| c.dead.is_empty() || !c.dead.contains(&(owner, e.other, e.rel.0)))
}

impl Adjacency {
    fn chunk(&self, owner: u32) -> Option<&DeltaChunk> {
        self.chunks.get((owner >> DELTA_BITS) as usize).map(|c| &**c)
    }

    fn chunk_mut(&mut self, owner: u32) -> &mut DeltaChunk {
        let index = (owner >> DELTA_BITS) as usize;
        if self.chunks.len() <= index {
            self.chunks.resize_with(index + 1, Default::default);
        }
        Arc::make_mut(&mut self.chunks[index])
    }

    pub(crate) fn find(&self, owner: u32, other: u32, rel: RelId) -> Option<EdgeRec> {
        let chunk = self.chunk(owner);
        if let Some(row) = chunk.and_then(|c| c.rows.get(&owner)) {
            if let Some(e) = row.iter().find(|e| matches(e, other, Some(rel))) {
                return Some(*e);
            }
        }
        self.base
            .row(owner)
            .iter()
            .find(|e| matches(e, other, Some(rel)) && base_live(chunk, owner, e))
            .copied()
    }

    /// Inserts or replaces the `(owner, rec.other, rec.rel)` entry.
    /// Returns `true` when an existing live entry was replaced.
    pub(crate) fn upsert(&mut self, owner: u32, rec: EdgeRec) -> bool {
        let in_base = self.base.row(owner).iter().any(|e| e.other == rec.other && e.rel == rec.rel);
        let chunk = self.chunk_mut(owner);
        let row = chunk.rows.entry(owner).or_default();
        if let Some(slot) = row.iter_mut().find(|e| e.other == rec.other && e.rel == rec.rel) {
            *slot = rec;
            return true;
        }
        row.push(rec);
        // A fresh tombstone means the base entry was live and is now shadowed.
        let replaced = in_base && chunk.dead.insert((owner, rec.other, rec.rel.0));
        self.delta_len += 1;
        if replaced {
            self.dead_len += 1;
        }
        replaced
    }

    /// Removes the entry for one relation, or every relation when `rel` is `None`.
    pub(crate) fn remove(&mut self, owner: u32, other: u32, rel: Option<RelId>) -> Vec<EdgeRec> {
        let chunk = self.chunk(owner);
        let base_hits: Vec<EdgeRec> = self
            .base
            .row(owner)
            .iter()
            .filter(|e| matches(e, other, rel) && base_live(chunk, owner, e))
            .copied()
            .collect();
        let delta_hit = chunk
            .and_then(|c| c.rows.get(&owner))
            .map_or(false, |row| row.iter().any(|e| matches(e, other, rel)));
        if base_hits.is_empty() && !delta_hit {
            return Vec::new();
        }

        let chunk = self.chunk_mut(owner);
        let mut removed = Vec::new();
        let mut now_empty = false;
        if let Some(row) = chunk.rows.get_mut(&owner) {
            row.retain(|e| {
                let hit = matches(e, other, rel);
                if hit {
                    removed.push(*e);
                }
                !hit
            });
            now_empty = row.is_empty();
        }
        if now_empty {
            chunk.rows.remove(&owner);
        }
        let from_delta = removed.len();
        let mut new_dead = 0;
        // Replaced base entries are already tombstoned, so base_hits only
        // holds live base edges and never double-counts a delta copy.
        for e in &base_hits {
            if chunk.dead.insert((owner, e.other, e.rel.0)) {
                new_dead += 1;
            }
        }
        removed.extend(base_hits);
        self.delta_len -= from_delta;
        self.dead_len += new_dead;
        removed
    }

    pub(crate) fn iter(&self, owner: u32) -> impl Iterator<Item = EdgeRec> + '_ {
        let chunk = self.chunk(owner);
        let base = self
            .base
            .row(owner)
            .iter()
            .filter(move |e| base_live(chunk, owner, e))
            .copied();
        let delta: &[EdgeRec] = chunk
            .and_then(|c| c.rows.get(&owner))
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        base.chain(delta.iter().copied())
    }

    pub(crate) fn degree(&self, owner: u32) -> usize {
        self.iter(owner).count()
    }

    pub(crate) fn pending(&self) -> usize {
        self.delta_len + self.dead_len
    }

    /// Folds delta and tombstones into a fresh CSR covering `rows` owners.
    pub(crate) fn compact(&mut self, rows: usize) {
        if self.pending() == 0 && self.base.offsets.len() == rows + 1 {
            return;
        }
        let mut offsets = Vec::with_capacity(rows + 1);
        let mut edges = Vec::new();
        offsets.push(0u32);
        for owner in 0..rows as u32 {
            let start = edges.len();
            edges.extend(self.iter(owner));
            edges[start..].sort_unstable_by_key(|e: &EdgeRec| (e.other, e.rel.0));
            offsets.push(u32::try_from(edges.len()).expect("layer exceeds u32::MAX edges"));
        }
        edges.shrink_to_fit();
        self.base = Arc::new(Csr { offsets, edges });
        self.chunks = Vec::new();
        self.delta_len = 0;
        self.dead_len = 0;
    }

    pub(crate) fn heap_bytes(&self) -> usize {
        let rec = std::mem::size_of::<EdgeRec>();
        let base = self.base.offsets.capacity() * 4 + self.base.edges.capacity() * rec;
        let delta: usize = self
            .chunks
            .iter()
            .map(|c| {
                let rows: usize = c.rows.values().map(|row| row.capacity() * rec + 40).sum();
                rows + c.dead.capacity() * 16 + 96
            })
            .sum();
        base + delta
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::OPEN_END;

    fn rec(other: u32, rel: u16, weight: f32) -> EdgeRec {
        EdgeRec {
            other,
            rel: RelId(rel),
            origin: 0,
            weight,
            confidence: 1.0,
            valid_from: 0,
            valid_to: OPEN_END,
        }
    }

    fn sorted(adj: &Adjacency, owner: u32) -> Vec<(u32, u16, f32)> {
        let mut v: Vec<_> = adj.iter(owner).map(|e| (e.other, e.rel.0, e.weight)).collect();
        v.sort_by(|a, b| (a.0, a.1).cmp(&(b.0, b.1)));
        v
    }

    #[test]
    fn upsert_replace_remove_survive_compaction() {
        let mut adj = Adjacency::default();
        assert!(!adj.upsert(0, rec(1, 0, 0.2)));
        assert!(!adj.upsert(0, rec(1, 1, 0.5)));
        assert!(adj.upsert(0, rec(1, 0, 0.9)));
        adj.compact(2);
        assert_eq!(adj.pending(), 0);
        assert_eq!(sorted(&adj, 0), vec![(1, 0, 0.9), (1, 1, 0.5)]);

        // Replacing a base entry tombstones it and shadows it from the delta.
        assert!(adj.upsert(0, rec(1, 0, 0.1)));
        assert_eq!(adj.degree(0), 2);
        assert_eq!(adj.find(0, 1, RelId(0)).unwrap().weight, 0.1);

        let removed = adj.remove(0, 1, Some(RelId(0)));
        assert_eq!(removed.len(), 1);
        assert_eq!(sorted(&adj, 0), vec![(1, 1, 0.5)]);
        assert!(adj.remove(0, 1, Some(RelId(0))).is_empty());

        // Re-adding a removed base triple is a fresh insert, not a replacement.
        assert!(!adj.upsert(0, rec(1, 0, 0.3)));
        adj.compact(2);
        assert_eq!(sorted(&adj, 0), vec![(1, 0, 0.3), (1, 1, 0.5)]);
        assert_eq!(adj.remove(0, 1, None).len(), 2);
        assert_eq!(adj.degree(0), 0);
        assert!(adj.iter(7).next().is_none());
    }

    #[test]
    fn clones_are_isolated_and_share_untouched_chunks() {
        let far = 5 << DELTA_BITS;
        let mut adj = Adjacency::default();
        adj.upsert(0, rec(1, 0, 1.0));
        adj.compact(2);
        adj.upsert(far, rec(1, 0, 1.0));
        let snapshot = adj.clone();

        adj.remove(0, 1, None);
        adj.upsert(0, rec(2, 0, 1.0));

        assert_eq!(sorted(&snapshot, 0), vec![(1, 0, 1.0)]);
        assert_eq!(sorted(&adj, 0), vec![(2, 0, 1.0)]);
        let far_chunk = (far >> DELTA_BITS) as usize;
        assert!(Arc::ptr_eq(&adj.chunks[far_chunk], &snapshot.chunks[far_chunk]));
        assert!(!Arc::ptr_eq(&adj.chunks[0], &snapshot.chunks[0]));
    }
}
