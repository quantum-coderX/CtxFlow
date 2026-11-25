//! Copy-on-write chunked vector.
//!
//! Cloning is O(len / CHUNK): only the chunk pointers are copied. A write to a
//! chunk that is shared with a published snapshot clones just that chunk.

use std::sync::Arc;

const CHUNK_BITS: usize = 10;
const CHUNK: usize = 1 << CHUNK_BITS;
const MASK: usize = CHUNK - 1;

#[derive(Clone, Debug)]
pub(crate) struct ChunkedVec<T> {
    chunks: Vec<Arc<Vec<T>>>,
    len: usize,
}

impl<T> Default for ChunkedVec<T> {
    fn default() -> Self {
        ChunkedVec { chunks: Vec::new(), len: 0 }
    }
}

impl<T: Clone> ChunkedVec<T> {
    pub(crate) fn len(&self) -> usize {
        self.len
    }

    pub(crate) fn push(&mut self, value: T) {
        if self.len & MASK == 0 {
            self.chunks.push(Arc::new(Vec::with_capacity(CHUNK)));
        }
        let last = self.chunks.last_mut().expect("chunk exists after push");
        Arc::make_mut(last).push(value);
        self.len += 1;
    }

    pub(crate) fn ensure_len(&mut self, len: usize, fill: T) {
        while self.len < len {
            self.push(fill.clone());
        }
    }

    pub(crate) fn get(&self, index: usize) -> Option<&T> {
        if index >= self.len {
            return None;
        }
        Some(&self.chunks[index >> CHUNK_BITS][index & MASK])
    }

    pub(crate) fn get_mut(&mut self, index: usize) -> Option<&mut T> {
        if index >= self.len {
            return None;
        }
        Some(&mut Arc::make_mut(&mut self.chunks[index >> CHUNK_BITS])[index & MASK])
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = &T> + '_ {
        self.chunks.iter().flat_map(|chunk| chunk.iter())
    }

    pub(crate) fn heap_bytes(&self) -> usize {
        let slots: usize = self.chunks.iter().map(|c| c.capacity()).sum();
        slots * std::mem::size_of::<T>() + self.chunks.capacity() * std::mem::size_of::<usize>()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clones_share_untouched_chunks() {
        let mut a = ChunkedVec::default();
        for i in 0..(CHUNK * 2 + 5) {
            a.push(i);
        }
        let b = a.clone();
        *a.get_mut(1).unwrap() = 999;

        assert_eq!(b.get(1), Some(&1));
        assert_eq!(a.get(1), Some(&999));
        assert!(Arc::ptr_eq(&a.chunks[1], &b.chunks[1]));
        assert!(!Arc::ptr_eq(&a.chunks[0], &b.chunks[0]));
        assert_eq!(a.iter().count(), CHUNK * 2 + 5);
        assert_eq!(a.get(CHUNK * 2 + 5), None);
    }
}
