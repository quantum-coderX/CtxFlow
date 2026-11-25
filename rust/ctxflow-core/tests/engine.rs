//! Actor, snapshot and WAL-recovery behaviour of `MemoryEngine`.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};

use ctxflow_core::{
    Command, EdgeInput, EngineConfig, Error, GraphConfig, Layer, MemoryEngine, NodeInput, Reply,
};

struct TempWal(PathBuf);

impl TempWal {
    fn new(name: &str) -> Self {
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let unique = format!(
            "ctxflow-{name}-{}-{}.wal",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::Relaxed)
        );
        let path = std::env::temp_dir().join(unique);
        let _ = std::fs::remove_file(&path);
        TempWal(path)
    }

    fn config(&self) -> EngineConfig {
        EngineConfig { wal_path: Some(self.0.clone()), sync_on_commit: false, ..EngineConfig::default() }
    }
}

impl Drop for TempWal {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

#[test]
fn writes_are_visible_in_the_next_snapshot_only() {
    let engine = MemoryEngine::start(EngineConfig::default()).unwrap();
    let handle = engine.handle();
    let before = handle.snapshot();

    handle.upsert_node(NodeInput::new("a")).unwrap();
    handle.upsert_node(NodeInput::new("b")).unwrap();
    assert!(!handle.upsert_edge(EdgeInput::new("a", "b", "derived_from")).unwrap());

    assert_eq!(before.node_count(), 0);
    let after = handle.snapshot();
    assert_eq!(after.node_count(), 2);
    assert!(after.has_edge("a", "b", Some("derived_from")));
    engine.shutdown().unwrap();
}

#[test]
fn batches_reply_in_order_with_per_command_errors() {
    let engine = MemoryEngine::start(EngineConfig::default()).unwrap();
    let results = engine.handle().execute_batch(vec![
        Command::UpsertNode(NodeInput::new("a")),
        Command::UpsertEdge(EdgeInput::new("a", "missing", "cites")),
        Command::UpsertNode(NodeInput::new("b")),
        Command::UpsertEdge(EdgeInput::new("a", "b", "cites")),
    ]);
    assert!(matches!(results[0], Ok(Reply::Node(_))));
    assert!(matches!(results[1], Err(Error::UnknownNode(_))));
    assert!(matches!(results[2], Ok(Reply::Node(_))));
    assert!(matches!(results[3], Ok(Reply::Edge(false))));
}

#[test]
fn concurrent_writers_and_readers() {
    let engine = MemoryEngine::start(EngineConfig {
        graph: GraphConfig { degree_cap: None, compact_threshold: 256, ..GraphConfig::default() },
        ..EngineConfig::default()
    })
    .unwrap();
    let handle = engine.handle();
    handle.upsert_node(NodeInput::new("hub")).unwrap();

    std::thread::scope(|scope| {
        for w in 0..4 {
            let h = handle.clone();
            scope.spawn(move || {
                for i in 0..250 {
                    let key = format!("w{w}-{i}");
                    h.upsert_node(NodeInput::new(key.clone())).unwrap();
                    h.upsert_edge(EdgeInput::new(key, "hub", "mentions")).unwrap();
                }
            });
        }
        for _ in 0..2 {
            let h = handle.clone();
            scope.spawn(move || {
                let mut last = 0;
                for _ in 0..200 {
                    let snap = h.snapshot();
                    // Snapshots are internally consistent and never go back in time.
                    assert!(snap.in_degree("hub") <= snap.node_count() - 1);
                    assert!(snap.node_count() >= last);
                    last = snap.node_count();
                }
            });
        }
    });

    let snap = handle.snapshot();
    assert_eq!(snap.node_count(), 1001);
    assert_eq!(snap.in_degree("hub"), 1000);
    assert_eq!(snap.layer_edge_count(Layer::Entity), 1000);
}

#[test]
fn wal_replay_restores_state_and_skips_rejected_commands() {
    let wal = TempWal::new("replay");
    {
        let engine = MemoryEngine::start(EngineConfig {
            graph: GraphConfig { degree_cap: Some(1), ..GraphConfig::default() },
            ..wal.config()
        })
        .unwrap();
        let h = engine.handle();
        h.register_relation("replicates", Layer::Causal, None, "").unwrap();
        for key in ["a", "b", "c"] {
            h.upsert_node(NodeInput::new(key).at(1)).unwrap();
        }
        h.upsert_edge(EdgeInput::new("a", "b", "replicates")).unwrap();
        assert!(h.upsert_edge(EdgeInput::new("a", "c", "cites")).is_err());
        h.supersede("b", "c", 5).unwrap();
        h.remove_node("a").unwrap();
        engine.shutdown().unwrap();
    }

    let engine = MemoryEngine::start(wal.config()).unwrap();
    let snap = engine.handle().snapshot();
    assert!(!snap.has_node("a"));
    assert_eq!(snap.node_count(), 2);
    assert_eq!(snap.node("b").unwrap().valid_to, 5);
    assert!(snap.has_edge("c", "b", Some("supersedes")));
    assert_eq!(snap.edge_count(), 1);
    let rel = snap.relation_id("replicates").unwrap();
    assert_eq!(snap.relation(rel).unwrap().layer, Layer::Causal);
    assert_eq!(snap.pending_delta(), 0);
}

#[test]
fn torn_wal_tail_is_truncated_on_open() {
    let wal = TempWal::new("torn");
    {
        let engine = MemoryEngine::start(wal.config()).unwrap();
        engine.handle().upsert_node(NodeInput::new("kept")).unwrap();
        engine.shutdown().unwrap();
    }
    let intact = std::fs::metadata(&wal.0).unwrap().len();
    {
        let mut file = OpenOptions::new().append(true).open(&wal.0).unwrap();
        file.write_all(&[42, 0, 0, 0, 1, 2]).unwrap();
    }

    let engine = MemoryEngine::start(wal.config()).unwrap();
    assert_eq!(std::fs::metadata(&wal.0).unwrap().len(), intact);
    let h = engine.handle();
    assert!(h.snapshot().has_node("kept"));
    h.upsert_node(NodeInput::new("after")).unwrap();
    engine.shutdown().unwrap();

    let engine = MemoryEngine::start(wal.config()).unwrap();
    assert_eq!(engine.handle().snapshot().node_count(), 2);
}

#[test]
fn foreign_files_are_not_treated_as_wal() {
    let wal = TempWal::new("foreign");
    std::fs::write(&wal.0, b"definitely not a wal").unwrap();
    assert!(matches!(MemoryEngine::start(wal.config()), Err(Error::Corrupt(_))));
}

#[test]
fn handles_fail_after_shutdown() {
    let engine = MemoryEngine::start(EngineConfig::default()).unwrap();
    let handle = engine.handle();
    engine.shutdown().unwrap();
    assert!(matches!(handle.upsert_node(NodeInput::new("x")), Err(Error::EngineStopped)));
    // The last snapshot stays readable.
    assert_eq!(handle.snapshot().node_count(), 0);
}
