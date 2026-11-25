//! Sorted creation-time index: an immutable sorted base plus an append delta.

use std::sync::Arc;

use crate::chunked::ChunkedVec;
use crate::model::Micros;

#[derive(Clone, Debug, Default)]
pub(crate) struct TimeIndex {
    base: Arc<Vec<(Micros, u32)>>,
    // Chunked so appends after a snapshot copy at most one chunk.
    delta: ChunkedVec<(Micros, u32)>,
}

impl TimeIndex {
    pub(crate) fn insert(&mut self, at: Micros, id: u32) {
        self.delta.push((at, id));
    }

    /// Ids with an entry in `[from, to)`. May contain stale entries for
    /// revived nodes; callers re-check the node record.
    pub(crate) fn range(&self, from: Micros, to: Micros) -> Vec<u32> {
        let lo = self.base.partition_point(|&(t, _)| t < from);
        let hi = self.base.partition_point(|&(t, _)| t < to).max(lo);
        let mut ids: Vec<u32> = self.base[lo..hi].iter().map(|&(_, id)| id).collect();
        ids.extend(
            self.delta
                .iter()
                .filter(|&&(t, _)| from <= t && t < to)
                .map(|&(_, id)| id),
        );
        ids
    }

    pub(crate) fn pending(&self) -> usize {
        self.delta.len()
    }

    pub(crate) fn compact(&mut self) {
        if self.delta.len() == 0 {
            return;
        }
        let mut merged = Vec::with_capacity(self.base.len() + self.delta.len());
        merged.extend_from_slice(&self.base);
        merged.extend(self.delta.iter().copied());
        merged.sort_unstable();
        merged.dedup();
        self.base = Arc::new(merged);
        self.delta = ChunkedVec::default();
    }

    pub(crate) fn heap_bytes(&self) -> usize {
        self.base.capacity() * std::mem::size_of::<(Micros, u32)>() + self.delta.heap_bytes()
    }
}
