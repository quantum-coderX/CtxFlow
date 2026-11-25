//! Relation registry and string interning shared by a graph and its snapshots.

use std::collections::HashMap;
use std::sync::Arc;

use crate::error::{Error, Result};
use crate::model::{Category, Layer, NodeId, RelId};

#[derive(Clone, Debug, PartialEq)]
pub struct RelationSpec {
    pub name: String,
    pub layer: Layer,
    pub category: Option<Category>,
    pub description: String,
}

/// Built-in relations. Categories match `ctxflow/vocabulary.py`; the layer
/// decides which adjacency store the edges live in.
pub const DEFAULT_RELATIONS: &[(&str, Layer, Option<Category>)] = &[
    ("follows", Layer::Temporal, Some(Category::Provenance)),
    ("supersedes", Layer::Temporal, Some(Category::Contradiction)),
    ("precedes", Layer::Temporal, None),
    ("derived_from", Layer::Provenance, Some(Category::Provenance)),
    ("extracted_from", Layer::Provenance, Some(Category::Provenance)),
    ("references", Layer::Provenance, Some(Category::Provenance)),
    ("cites", Layer::Provenance, Some(Category::Research)),
    ("summary_of", Layer::Provenance, Some(Category::Structural)),
    ("summarizes", Layer::Provenance, Some(Category::Structural)),
    ("duplicate_of", Layer::Provenance, Some(Category::Structural)),
    ("caused_by", Layer::Causal, Some(Category::Structural)),
    ("causes", Layer::Causal, Some(Category::Structural)),
    ("depends_on", Layer::Causal, Some(Category::Structural)),
    ("based_on", Layer::Causal, Some(Category::Structural)),
    ("supports", Layer::Causal, Some(Category::Research)),
    ("extends", Layer::Causal, Some(Category::Research)),
    ("contradicts", Layer::Causal, Some(Category::Contradiction)),
    ("related_to", Layer::Entity, Some(Category::Structural)),
    ("evaluated_on", Layer::Entity, Some(Category::Research)),
    ("mentions", Layer::Entity, None),
    ("is_a", Layer::Entity, None),
    ("part_of", Layer::Entity, None),
];

/// Layer used for relations that were never registered.
pub const DEFAULT_LAYER: Layer = Layer::Entity;

#[derive(Clone, Debug, Default)]
pub struct Vocabulary {
    specs: Vec<RelationSpec>,
    by_name: HashMap<String, RelId>,
}

impl Vocabulary {
    pub fn with_defaults() -> Self {
        let mut vocab = Vocabulary::default();
        for (name, layer, category) in DEFAULT_RELATIONS {
            vocab
                .register(name, *layer, *category, "")
                .expect("built-in relations are valid");
        }
        vocab
    }

    /// Registers a relation or updates its category/description. A relation's
    /// layer is fixed once registered because existing edges are stored there.
    pub fn register(
        &mut self,
        name: &str,
        layer: Layer,
        category: Option<Category>,
        description: &str,
    ) -> Result<RelId> {
        let name = name.trim();
        if name.is_empty() {
            return Err(Error::InvalidInput("relation name must be non-empty".into()));
        }
        if let Some(&id) = self.by_name.get(name) {
            let spec = &mut self.specs[id.0 as usize];
            if spec.layer != layer {
                return Err(Error::LayerChange {
                    relation: name.to_string(),
                    from: spec.layer,
                    to: layer,
                });
            }
            spec.category = category;
            spec.description = description.to_string();
            return Ok(id);
        }
        if self.specs.len() > u16::MAX as usize {
            return Err(Error::SymbolOverflow("relation"));
        }
        let id = RelId(self.specs.len() as u16);
        self.specs.push(RelationSpec {
            name: name.to_string(),
            layer,
            category,
            description: description.to_string(),
        });
        self.by_name.insert(name.to_string(), id);
        Ok(id)
    }

    pub(crate) fn resolve_or_register(&mut self, name: &str) -> Result<RelId> {
        match self.resolve(name) {
            Some(id) => Ok(id),
            None => self.register(name, DEFAULT_LAYER, None, ""),
        }
    }

    pub fn resolve(&self, name: &str) -> Option<RelId> {
        self.by_name.get(name.trim()).copied()
    }

    pub fn spec(&self, id: RelId) -> Option<&RelationSpec> {
        self.specs.get(id.0 as usize)
    }

    pub fn category(&self, id: RelId) -> Option<Category> {
        self.spec(id).and_then(|s| s.category)
    }

    pub fn relations_in(&self, category: Category) -> Vec<RelId> {
        (0..self.specs.len())
            .filter(|&i| self.specs[i].category == Some(category))
            .map(|i| RelId(i as u16))
            .collect()
    }

    pub fn len(&self) -> usize {
        self.specs.len()
    }

    pub fn is_empty(&self) -> bool {
        self.specs.is_empty()
    }
}

/// Small string table for low-cardinality symbols (node types, edge origins).
#[derive(Clone, Debug, Default)]
pub(crate) struct SmallInterner {
    names: Vec<String>,
    by_name: HashMap<String, u16>,
}

impl SmallInterner {
    pub(crate) fn intern(&mut self, name: &str, what: &'static str) -> Result<u16> {
        if let Some(&id) = self.by_name.get(name) {
            return Ok(id);
        }
        if self.names.len() > u16::MAX as usize {
            return Err(Error::SymbolOverflow(what));
        }
        let id = self.names.len() as u16;
        self.names.push(name.to_string());
        self.by_name.insert(name.to_string(), id);
        Ok(id)
    }

    pub(crate) fn name(&self, id: u16) -> Option<&str> {
        self.names.get(id as usize).map(String::as_str)
    }
}

/// Append-only symbol tables. Ids are never reassigned, so a snapshot can
/// share this table with the live graph; ids newer than a snapshot's node
/// table simply resolve to "absent" in that snapshot.
#[derive(Debug)]
pub struct Symbols {
    keys: Vec<Arc<str>>,
    by_key: HashMap<Arc<str>, NodeId>,
    pub(crate) vocab: Vocabulary,
    pub(crate) node_types: SmallInterner,
    pub(crate) origins: SmallInterner,
}

impl Default for Symbols {
    fn default() -> Self {
        let mut origins = SmallInterner::default();
        for name in ["rule", "user", "llm", "laya"] {
            origins.intern(name, "origin").expect("built-in origins fit");
        }
        let mut node_types = SmallInterner::default();
        node_types.intern("generic", "node type").expect("built-in type fits");
        Symbols {
            keys: Vec::new(),
            by_key: HashMap::new(),
            vocab: Vocabulary::with_defaults(),
            node_types,
            origins,
        }
    }
}

impl Symbols {
    pub(crate) fn intern_key(&mut self, key: &str) -> Result<NodeId> {
        if let Some(&id) = self.by_key.get(key) {
            return Ok(id);
        }
        if self.keys.len() >= u32::MAX as usize {
            return Err(Error::SymbolOverflow("node key"));
        }
        let id = NodeId(self.keys.len() as u32);
        let shared: Arc<str> = Arc::from(key);
        self.keys.push(shared.clone());
        self.by_key.insert(shared, id);
        Ok(id)
    }

    pub(crate) fn lookup_key(&self, key: &str) -> Option<NodeId> {
        self.by_key.get(key).copied()
    }

    pub(crate) fn key(&self, id: NodeId) -> Option<Arc<str>> {
        self.keys.get(id.0 as usize).cloned()
    }

    /// Rough heap estimate (strings plus map/vector slots).
    pub(crate) fn heap_bytes(&self) -> usize {
        let strings: usize = self.keys.iter().map(|k| k.len() + 16).sum();
        strings + self.keys.capacity() * 16 + self.by_key.capacity() * 24
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_classifies_and_extends() {
        let mut vocab = Vocabulary::with_defaults();
        let supersedes = vocab.resolve("supersedes").unwrap();
        assert_eq!(vocab.category(supersedes), Some(Category::Contradiction));
        assert!(vocab.resolve("unknown_rel").is_none());
        let derived = vocab.resolve("derived_from").unwrap();
        assert!(vocab.relations_in(Category::Provenance).contains(&derived));

        let replicates = vocab
            .register("replicates", Layer::Causal, Some(Category::Research), "Replicates a result")
            .unwrap();
        assert!(vocab.relations_in(Category::Research).contains(&replicates));
    }

    #[test]
    fn relation_layer_is_fixed_once_registered() {
        let mut vocab = Vocabulary::with_defaults();
        let err = vocab.register("cites", Layer::Entity, None, "").unwrap_err();
        assert!(matches!(err, Error::LayerChange { .. }));
        assert!(vocab.register("cites", Layer::Provenance, None, "").is_ok());
        assert!(vocab.register("  ", Layer::Entity, None, "").is_err());
    }

    #[test]
    fn unknown_relations_default_to_entity_layer() {
        let mut vocab = Vocabulary::with_defaults();
        let id = vocab.resolve_or_register("collaborates_with").unwrap();
        assert_eq!(vocab.spec(id).unwrap().layer, Layer::Entity);
        assert_eq!(vocab.category(id), None);
    }
}
