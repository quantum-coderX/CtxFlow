//! Memory and latency benchmark for the compact graph.
//!
//!     cargo run --release --example mem_bench -- [nodes] [out_degree]
//!
//! Heap usage is measured exactly with a counting global allocator. Compare
//! with `python scripts/bench_graph_memory.py` for the networkx baseline.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

use ctxflow_core::{Direction, EdgeInput, Graph, GraphConfig, NodeInput, OPEN_END};

struct Counting;

static LIVE: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = System.alloc(layout);
        if !ptr.is_null() {
            LIVE.fetch_add(layout.size(), Ordering::Relaxed);
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout);
        LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let new_ptr = System.realloc(ptr, layout, new_size);
        if !new_ptr.is_null() {
            LIVE.fetch_add(new_size, Ordering::Relaxed);
            LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
        }
        new_ptr
    }
}

#[global_allocator]
static GLOBAL: Counting = Counting;

fn live() -> usize {
    LIVE.load(Ordering::Relaxed)
}

/// Same deterministic generator as scripts/bench_graph_memory.py.
struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0 >> 33
    }
}

const RELATIONS: [&str; 4] = ["follows", "derived_from", "causes", "mentions"];

fn p95(mut samples: Vec<f64>) -> f64 {
    samples.sort_by(|a, b| a.total_cmp(b));
    samples[((samples.len() as f64 * 0.95).ceil() as usize).saturating_sub(1)]
}

fn main() {
    let mut args = std::env::args().skip(1);
    let nodes: usize = args.next().and_then(|s| s.parse().ok()).unwrap_or(100_000);
    let degree: usize = args.next().and_then(|s| s.parse().ok()).unwrap_or(8);
    let key = |i: usize| format!("node-{i:08}");

    let baseline = live();
    let mut graph = Graph::new(GraphConfig {
        degree_cap: None,
        compact_threshold: usize::MAX,
        ..GraphConfig::default()
    });

    let started = Instant::now();
    for i in 0..nodes {
        graph.upsert_node(&NodeInput::new(key(i)).at(i as i64)).unwrap();
    }
    let mut rng = Lcg(42);
    for i in 0..nodes {
        for j in 0..degree {
            let dst = (rng.next() % nodes as u64) as usize;
            let edge = EdgeInput::new(key(i), key(dst), RELATIONS[j % RELATIONS.len()])
                .valid(i as i64, OPEN_END);
            graph.upsert_edge(&edge).unwrap();
        }
    }
    let ingest = started.elapsed();
    let edges = graph.edge_count();
    let delta_bytes = live() - baseline;

    let started = Instant::now();
    graph.compact();
    let compact = started.elapsed();
    let compact_bytes = live() - baseline;

    let mut bfs = Vec::new();
    let mut prov = Vec::new();
    let mut causal = Vec::new();
    for _ in 0..1000 {
        let k = key((rng.next() % nodes as u64) as usize);
        let t = Instant::now();
        std::hint::black_box(graph.bfs_distances(&k, 2, &[]));
        bfs.push(t.elapsed().as_secs_f64() * 1e3);
        let t = Instant::now();
        std::hint::black_box(graph.trace_provenance(&k, 10));
        prov.push(t.elapsed().as_secs_f64() * 1e3);
        let t = Instant::now();
        std::hint::black_box(graph.causal_walk(&k, Direction::Out, 4, 0.0, None));
        causal.push(t.elapsed().as_secs_f64() * 1e3);
    }

    println!("{{");
    println!("  \"engine\": \"ctxflow-core\",");
    println!("  \"nodes\": {nodes},");
    println!("  \"edges\": {edges},");
    println!("  \"heap_bytes_before_compaction\": {delta_bytes},");
    println!("  \"heap_bytes\": {compact_bytes},");
    println!("  \"bytes_per_edge\": {:.1},", compact_bytes as f64 / edges.max(1) as f64);
    println!("  \"estimated_heap_bytes\": {},", graph.heap_bytes());
    println!("  \"ingest_s\": {:.3},", ingest.as_secs_f64());
    println!("  \"compact_s\": {:.3},", compact.as_secs_f64());
    println!("  \"bfs2_p95_ms\": {:.4},", p95(bfs));
    println!("  \"provenance_p95_ms\": {:.4},", p95(prov));
    println!("  \"causal4_p95_ms\": {:.4}", p95(causal));
    println!("}}");
}
