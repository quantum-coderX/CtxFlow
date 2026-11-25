//! Parity with tests/test_multigraph.py plus layer, temporal and causal behaviour.

use ctxflow_core::{
    Category, Direction, EdgeInput, Error, Graph, GraphConfig, Layer, NodeInput, NodeStatus, OPEN_END,
};

fn graph_with(config: GraphConfig) -> Graph {
    let mut graph = Graph::new(config);
    for key in ["a", "b", "c"] {
        graph.upsert_node(&NodeInput::new(key).at(0)).unwrap();
    }
    graph
}

fn graph() -> Graph {
    graph_with(GraphConfig::default())
}

fn relations(graph: &Graph, src: &str, dst: &str) -> Vec<String> {
    let mut names: Vec<String> = graph
        .edges_between(src, dst)
        .iter()
        .map(|e| graph.relation_name(e.relation).unwrap())
        .collect();
    names.sort();
    names
}

#[test]
fn parallel_relations_coexist_between_one_pair() {
    let mut g = graph();
    g.upsert_edge(&EdgeInput::new("a", "b", "cites")).unwrap();
    g.upsert_edge(&EdgeInput::new("a", "b", "supports")).unwrap();

    assert_eq!(g.edge_count(), 2);
    assert_eq!(relations(&g, "a", "b"), ["cites", "supports"]);
    assert!(g.has_edge("a", "b", Some("cites")));
    assert!(!g.has_edge("a", "b", Some("extends")));
    // Different relations land in their own layers.
    assert_eq!(g.layer_edge_count(Layer::Provenance), 1);
    assert_eq!(g.layer_edge_count(Layer::Causal), 1);
}

#[test]
fn same_triple_replaces_instead_of_duplicating() {
    let mut g = graph();
    assert!(!g.upsert_edge(&EdgeInput::new("a", "b", "cites").with_weight(0.2)).unwrap());
    assert!(g.upsert_edge(&EdgeInput::new("a", "b", "cites").with_weight(0.9)).unwrap());

    let edges = g.edges_between("a", "b");
    assert_eq!(edges.len(), 1);
    assert_eq!(edges[0].weight, 0.9);
    assert_eq!(g.edge_count(), 1);
}

#[test]
fn remove_edge_targets_one_relation_or_all() {
    let mut g = graph();
    g.upsert_edge(&EdgeInput::new("a", "b", "cites")).unwrap();
    g.upsert_edge(&EdgeInput::new("a", "b", "supports")).unwrap();

    assert_eq!(g.remove_edge("a", "b", Some("cites")), 1);
    assert_eq!(relations(&g, "a", "b"), ["supports"]);
    assert_eq!(g.remove_edge("a", "b", Some("cites")), 0);
    assert_eq!(g.remove_edge("a", "b", None), 1);
    assert_eq!(g.edge_count(), 0);
    assert!(g.in_edges("b", None).is_empty());
}

#[test]
fn edge_confidence_and_origin_are_kept() {
    let mut g = graph();
    g.upsert_edge(&EdgeInput::new("a", "b", "cites").with_confidence(0.7).with_origin("laya"))
        .unwrap();
    g.upsert_edge(&EdgeInput::new("a", "b", "supports")).unwrap();
    g.compact();

    for e in g.edges_between("a", "b") {
        match g.relation_name(e.relation).unwrap().as_str() {
            "cites" => {
                assert_eq!(e.confidence, 0.7);
                assert_eq!(g.origin_name(e.origin).unwrap(), "laya");
            }
            _ => assert_eq!(g.origin_name(e.origin).unwrap(), "rule"),
        }
    }
}

#[test]
fn degree_cap_counts_parallel_relations_but_allows_replacement() {
    let mut g = graph_with(GraphConfig { degree_cap: Some(2), ..GraphConfig::default() });

    g.upsert_edge(&EdgeInput::new("a", "b", "cites")).unwrap();
    g.upsert_edge(&EdgeInput::new("a", "b", "supports")).unwrap();
    let err = g.upsert_edge(&EdgeInput::new("a", "c", "cites")).unwrap_err();
    assert!(matches!(err, Error::DegreeCapExceeded { cap: 2, .. }));
    // A rejected edge must not register its relation (it is never logged).
    assert!(g.upsert_edge(&EdgeInput::new("a", "c", "brand_new_rel")).is_err());
    assert!(g.relation_id("brand_new_rel").is_none());
    // Replacing an existing triple never grows the fan-out.
    assert!(g.upsert_edge(&EdgeInput::new("a", "b", "cites").with_weight(0.5)).unwrap());
}

#[test]
fn provenance_walk_uses_registry_categories() {
    let mut g = graph();
    g.upsert_edge(&EdgeInput::new("c", "b", "derived_from")).unwrap();
    g.upsert_edge(&EdgeInput::new("c", "b", "related_to")).unwrap();
    g.upsert_edge(&EdgeInput::new("b", "a", "supersedes")).unwrap();

    let chain = g.trace_provenance("b", 10).unwrap();
    let steps: Vec<_> = chain.steps.iter().map(|s| g.key(s.node).unwrap().to_string()).collect();
    let contra: Vec<_> = chain.contradictions.iter().map(|(n, _)| g.key(*n).unwrap().to_string()).collect();

    assert_eq!(steps, ["b", "c"]);
    assert_eq!(contra, ["a"]);
    assert!(g.trace_provenance("missing", 10).is_none());
}

#[test]
fn registered_relations_drive_layer_and_category() {
    let mut g = graph();
    g.register_relation("replicates", Layer::Causal, Some(Category::Provenance), "").unwrap();
    g.upsert_edge(&EdgeInput::new("c", "b", "replicates")).unwrap();
    assert_eq!(g.layer_edge_count(Layer::Causal), 1);
    let chain = g.trace_provenance("b", 10).unwrap();
    assert_eq!(chain.steps.len(), 2);

    // Unregistered relations are accepted into the entity layer.
    g.upsert_edge(&EdgeInput::new("a", "c", "collaborates_with")).unwrap();
    assert_eq!(g.layer_edge_count(Layer::Entity), 1);
}

#[test]
fn removing_a_node_drops_incident_edges_in_every_layer() {
    let mut g = graph();
    g.upsert_edge(&EdgeInput::new("a", "b", "follows")).unwrap();
    g.upsert_edge(&EdgeInput::new("b", "c", "derived_from")).unwrap();
    g.upsert_edge(&EdgeInput::new("c", "b", "supports")).unwrap();
    g.upsert_edge(&EdgeInput::new("b", "b", "related_to")).unwrap();
    g.compact();

    assert!(g.remove_node("b"));
    assert!(!g.has_node("b"));
    assert_eq!(g.node_count(), 2);
    assert_eq!(g.edge_count(), 0);
    assert!(g.out_edges("a", None).is_empty());
    assert!(g.in_edges("c", None).is_empty());
    assert!(!g.remove_node("b"));

    // Re-adding the key revives the slot with a clean state.
    g.upsert_node(&NodeInput::new("b").at(5)).unwrap();
    assert_eq!(g.node("b").unwrap().status, NodeStatus::Active);
    assert_eq!(g.out_degree("b"), 0);
}

#[test]
fn compaction_preserves_every_query() {
    let mut g = graph_with(GraphConfig { degree_cap: None, ..GraphConfig::default() });
    for i in 0..200 {
        g.upsert_node(&NodeInput::new(format!("n{i}")).at(i)).unwrap();
    }
    for i in 0..200 {
        for step in [1, 7, 31] {
            let dst = format!("n{}", (i + step) % 200);
            let rel = ["follows", "derived_from", "causes", "mentions"][(i as usize + step as usize) % 4];
            g.upsert_edge(&EdgeInput::new(format!("n{i}"), dst, rel)).unwrap();
        }
    }
    g.remove_edge("n0", "n1", None);
    g.remove_node("n50");

    let before: Vec<_> = (0..200).map(|i| (g.out_degree(&format!("n{i}")), g.in_degree(&format!("n{i}")))).collect();
    let bfs_before = g.bfs_distances("n3", 2, &[]);
    let edges_before = g.edge_count();
    g.compact();
    assert_eq!(g.pending_delta(), 0);
    let after: Vec<_> = (0..200).map(|i| (g.out_degree(&format!("n{i}")), g.in_degree(&format!("n{i}")))).collect();

    assert_eq!(before, after);
    assert_eq!(bfs_before, g.bfs_distances("n3", 2, &[]));
    assert_eq!(edges_before, g.edge_count());
}

#[test]
fn supersession_closes_validity_and_links_temporally() {
    let mut g = Graph::default();
    g.upsert_node(&NodeInput::new("old").at(10)).unwrap();
    g.upsert_node(&NodeInput::new("new").at(20)).unwrap();
    g.supersede("old", "new", 20).unwrap();

    let old = g.node("old").unwrap();
    assert_eq!(old.status, NodeStatus::Superseded);
    assert_eq!(old.superseded_by, g.id("new"));
    assert_eq!(old.valid_to, 20);
    assert!(g.has_edge("new", "old", Some("supersedes")));
    assert_eq!(g.layer_edge_count(Layer::Temporal), 1);

    assert_eq!(g.nodes_valid_at(15), vec![g.id("old").unwrap()]);
    assert_eq!(g.nodes_valid_at(25), vec![g.id("new").unwrap()]);
    assert_eq!(g.nodes_by_status(NodeStatus::Superseded), vec![g.id("old").unwrap()]);
    assert!(matches!(g.supersede("old", "old", 30), Err(Error::InvalidInput(_))));
}

#[test]
fn creation_time_index_answers_ranges_before_and_after_compaction() {
    let mut g = Graph::default();
    for (key, t) in [("x", 30), ("y", 10), ("z", 20)] {
        g.upsert_node(&NodeInput::new(key).at(t)).unwrap();
    }
    let keys = |g: &Graph, from, to| -> Vec<String> {
        g.nodes_created_between(from, to).into_iter().map(|id| g.key(id).unwrap().to_string()).collect()
    };
    assert_eq!(keys(&g, 10, 30), ["y", "z"]);
    g.compact();
    assert_eq!(keys(&g, 0, 100), ["y", "z", "x"]);
    g.remove_node("z");
    assert_eq!(keys(&g, 0, 100), ["y", "x"]);
}

#[test]
fn edges_as_of_respects_edge_and_node_validity() {
    let mut g = Graph::default();
    g.upsert_node(&NodeInput::new("p").at(0)).unwrap();
    g.upsert_node(&NodeInput::new("q").at(0).valid_until(50)).unwrap();
    g.upsert_node(&NodeInput::new("r").at(0)).unwrap();
    g.upsert_edge(&EdgeInput::new("p", "q", "mentions").valid(0, OPEN_END)).unwrap();
    g.upsert_edge(&EdgeInput::new("p", "r", "mentions").valid(10, 20)).unwrap();

    let at = |t| g.edges_as_of("p", Direction::Out, Some(Layer::Entity), t).len();
    assert_eq!(at(5), 1);
    assert_eq!(at(15), 2);
    assert_eq!(at(60), 0);
}

#[test]
fn causal_walk_multiplies_confidence_and_skips_contradictions() {
    let mut g = Graph::new(GraphConfig { min_inferred_causal_confidence: 0.6, ..GraphConfig::default() });
    for key in ["s", "m", "t", "x"] {
        g.upsert_node(&NodeInput::new(key).at(0)).unwrap();
    }
    g.upsert_edge(&EdgeInput::new("s", "m", "causes").with_confidence(0.9)).unwrap();
    g.upsert_edge(&EdgeInput::new("m", "t", "causes").with_confidence(0.8).with_origin("llm")).unwrap();
    g.upsert_edge(&EdgeInput::new("s", "x", "contradicts")).unwrap();

    // Inferred causal edges below the configured floor are rejected.
    let low = EdgeInput::new("s", "t", "causes").with_confidence(0.3).with_origin("llm");
    assert!(matches!(g.upsert_edge(&low), Err(Error::ConfidenceTooLow { .. })));

    let hops = g.causal_walk("s", Direction::Out, 5, 0.0, None);
    let found: Vec<(String, u32, f32)> = hops
        .iter()
        .map(|h| (g.key(h.node).unwrap().to_string(), h.depth, h.confidence))
        .collect();
    assert_eq!(found.len(), 2);
    assert_eq!((found[0].0.as_str(), found[0].1), ("m", 1));
    assert_eq!((found[1].0.as_str(), found[1].1), ("t", 2));
    assert!((found[1].2 - 0.72).abs() < 1e-6);

    assert_eq!(g.causal_walk("s", Direction::Out, 5, 0.8, None).len(), 1);
    let upstream = g.causal_walk("t", Direction::In, 5, 0.0, None);
    assert_eq!(upstream.len(), 2);
}

#[test]
fn invalid_inputs_are_rejected() {
    let mut g = graph();
    assert!(matches!(g.upsert_edge(&EdgeInput::new("a", "zzz", "cites")), Err(Error::UnknownNode(_))));
    assert!(matches!(
        g.upsert_edge(&EdgeInput::new("a", "b", "cites").with_confidence(1.5)),
        Err(Error::InvalidInput(_))
    ));
    assert!(matches!(g.upsert_node(&NodeInput::new("")), Err(Error::InvalidInput(_))));
    assert!(matches!(
        g.upsert_node(&NodeInput::new("bad").at(10).valid_until(5)),
        Err(Error::InvalidInput(_))
    ));
}

#[test]
fn clones_are_isolated_snapshots() {
    let mut g = graph();
    g.upsert_edge(&EdgeInput::new("a", "b", "cites")).unwrap();
    let snapshot = g.clone();

    g.upsert_node(&NodeInput::new("d")).unwrap();
    g.upsert_edge(&EdgeInput::new("a", "d", "cites")).unwrap();
    g.remove_edge("a", "b", None);

    assert!(snapshot.has_edge("a", "b", Some("cites")));
    assert!(!snapshot.has_node("d"));
    assert_eq!(snapshot.edge_count(), 1);
    assert!(!g.has_edge("a", "b", None));
}
