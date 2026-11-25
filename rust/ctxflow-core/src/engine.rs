//! Single-writer actor with snapshot readers.
//!
//! One writer thread owns the mutable [`Graph`]. Commands arrive on a bounded
//! mailbox (backpressure), are applied in batches, logged to the WAL, and
//! acknowledged only after the batch commits (group commit). After each
//! batch the writer publishes an immutable `Arc<Graph>` snapshot; readers
//! query snapshots without ever blocking the writer.

use std::path::PathBuf;
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, RwLock};
use std::thread::JoinHandle;

use crate::error::{Error, Result};
use crate::graph::{Graph, GraphConfig};
use crate::model::{Category, EdgeInput, Layer, Micros, NodeId, NodeInput, RelId};
use crate::wal::Wal;

#[derive(Clone, Debug, PartialEq)]
pub enum Command {
    UpsertNode(NodeInput),
    RemoveNode(String),
    UpsertEdge(EdgeInput),
    RemoveEdge { src: String, dst: String, relation: Option<String> },
    Supersede { old: String, new: String, at: Micros },
    RegisterRelation { name: String, layer: Layer, category: Option<Category>, description: String },
    Compact,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Reply {
    Node(NodeId),
    /// `true` when an existing edge was replaced.
    Edge(bool),
    Removed(usize),
    Relation(RelId),
    Done,
}

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub graph: GraphConfig,
    /// Durable log location; `None` keeps the engine purely in memory.
    pub wal_path: Option<PathBuf>,
    /// `fsync` on every batch commit (otherwise flush to the OS only).
    pub sync_on_commit: bool,
    /// Maximum commands applied per batch.
    pub max_batch: usize,
    /// Bounded mailbox capacity.
    pub mailbox: usize,
}

impl Default for EngineConfig {
    fn default() -> Self {
        EngineConfig {
            graph: GraphConfig::default(),
            wal_path: None,
            sync_on_commit: true,
            max_batch: 1024,
            mailbox: 4096,
        }
    }
}

enum Msg {
    Cmd(Command),
    Shutdown,
}

struct Envelope {
    msg: Msg,
    reply: SyncSender<Result<Reply>>,
}

/// Cloneable, thread-safe client of a running engine.
#[derive(Clone)]
pub struct EngineHandle {
    tx: SyncSender<Envelope>,
    published: Arc<RwLock<Arc<Graph>>>,
}

impl EngineHandle {
    /// Latest committed snapshot. Cheap: one read-lock and an `Arc` clone.
    pub fn snapshot(&self) -> Arc<Graph> {
        self.published.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// Applies one command and waits until it is committed.
    pub fn execute(&self, command: Command) -> Result<Reply> {
        self.submit(Msg::Cmd(command))?.recv().map_err(|_| Error::EngineStopped)?
    }

    /// Pipelines many commands; results come back in order. Commands sent
    /// together usually share one batch and one WAL commit.
    pub fn execute_batch(&self, commands: Vec<Command>) -> Vec<Result<Reply>> {
        let pending: Vec<Result<Receiver<Result<Reply>>>> =
            commands.into_iter().map(|c| self.submit(Msg::Cmd(c))).collect();
        pending
            .into_iter()
            .map(|rx| rx?.recv().map_err(|_| Error::EngineStopped)?)
            .collect()
    }

    fn submit(&self, msg: Msg) -> Result<Receiver<Result<Reply>>> {
        let (reply, rx) = sync_channel(1);
        self.tx.send(Envelope { msg, reply }).map_err(|_| Error::EngineStopped)?;
        Ok(rx)
    }

    pub fn upsert_node(&self, input: NodeInput) -> Result<NodeId> {
        match self.execute(Command::UpsertNode(input))? {
            Reply::Node(id) => Ok(id),
            other => Err(unexpected(other)),
        }
    }

    pub fn upsert_edge(&self, input: EdgeInput) -> Result<bool> {
        match self.execute(Command::UpsertEdge(input))? {
            Reply::Edge(replaced) => Ok(replaced),
            other => Err(unexpected(other)),
        }
    }

    pub fn remove_node(&self, key: impl Into<String>) -> Result<bool> {
        match self.execute(Command::RemoveNode(key.into()))? {
            Reply::Removed(n) => Ok(n > 0),
            other => Err(unexpected(other)),
        }
    }

    pub fn remove_edge(&self, src: impl Into<String>, dst: impl Into<String>, relation: Option<&str>) -> Result<usize> {
        let command = Command::RemoveEdge {
            src: src.into(),
            dst: dst.into(),
            relation: relation.map(str::to_string),
        };
        match self.execute(command)? {
            Reply::Removed(n) => Ok(n),
            other => Err(unexpected(other)),
        }
    }

    pub fn supersede(&self, old: impl Into<String>, new: impl Into<String>, at: Micros) -> Result<()> {
        self.execute(Command::Supersede { old: old.into(), new: new.into(), at }).map(|_| ())
    }

    pub fn register_relation(
        &self,
        name: impl Into<String>,
        layer: Layer,
        category: Option<Category>,
        description: impl Into<String>,
    ) -> Result<RelId> {
        let command = Command::RegisterRelation {
            name: name.into(),
            layer,
            category,
            description: description.into(),
        };
        match self.execute(command)? {
            Reply::Relation(id) => Ok(id),
            other => Err(unexpected(other)),
        }
    }

    pub fn compact(&self) -> Result<()> {
        self.execute(Command::Compact).map(|_| ())
    }
}

fn unexpected(reply: Reply) -> Error {
    Error::InvalidInput(format!("unexpected engine reply {reply:?}"))
}

/// Owns the writer thread. Dropping it shuts the writer down cleanly.
pub struct MemoryEngine {
    handle: EngineHandle,
    writer: Option<JoinHandle<()>>,
}

impl MemoryEngine {
    /// Starts the writer. With a WAL path, the log is replayed first.
    pub fn start(config: EngineConfig) -> Result<MemoryEngine> {
        let mut graph = Graph::new(config.graph.clone());
        let wal = match &config.wal_path {
            Some(path) => {
                let (wal, commands) = Wal::open(path, config.sync_on_commit)?;
                for command in &commands {
                    apply(&mut graph, command, false)?;
                }
                graph.compact();
                Some(wal)
            }
            None => None,
        };

        let published = Arc::new(RwLock::new(Arc::new(graph.clone())));
        let (tx, rx) = sync_channel(config.mailbox.max(1));
        let writer = Writer {
            graph,
            wal,
            rx,
            published: Arc::clone(&published),
            max_batch: config.max_batch.max(1),
        };
        let thread = std::thread::Builder::new()
            .name("ctxflow-writer".into())
            .spawn(move || writer.run())?;

        Ok(MemoryEngine {
            handle: EngineHandle { tx, published },
            writer: Some(thread),
        })
    }

    pub fn handle(&self) -> EngineHandle {
        self.handle.clone()
    }

    /// Commits everything queued before this call, then stops the writer.
    pub fn shutdown(mut self) -> Result<()> {
        self.stop()
    }

    fn stop(&mut self) -> Result<()> {
        let Some(thread) = self.writer.take() else {
            return Ok(());
        };
        let acked = self
            .handle
            .submit(Msg::Shutdown)
            .and_then(|rx| rx.recv().map_err(|_| Error::EngineStopped)?);
        let joined = thread.join();
        if joined.is_err() {
            return Err(Error::Wal("writer thread panicked".into()));
        }
        // A writer that already stopped (e.g. fail-stop after a WAL error)
        // cannot acknowledge; that is not a shutdown failure.
        match acked {
            Ok(_) | Err(Error::EngineStopped) => Ok(()),
            Err(err) => Err(err),
        }
    }
}

impl Drop for MemoryEngine {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

struct Writer {
    graph: Graph,
    wal: Option<Wal>,
    rx: Receiver<Envelope>,
    published: Arc<RwLock<Arc<Graph>>>,
    max_batch: usize,
}

impl Writer {
    fn run(mut self) {
        while let Ok(first) = self.rx.recv() {
            let mut batch = vec![first];
            while batch.len() < self.max_batch {
                match self.rx.try_recv() {
                    Ok(env) => batch.push(env),
                    Err(_) => break,
                }
            }

            let mut replies = Vec::with_capacity(batch.len());
            let mut stop = false;
            let mut wal_error: Option<String> = None;
            for env in batch {
                let result = match env.msg {
                    Msg::Shutdown => {
                        stop = true;
                        Ok(Reply::Done)
                    }
                    Msg::Cmd(command) => {
                        let result = apply(&mut self.graph, &command, true);
                        if result.is_ok() && wal_error.is_none() {
                            if let Some(wal) = self.wal.as_mut() {
                                if let Err(err) = wal.append(&command) {
                                    wal_error = Some(err.to_string());
                                }
                            }
                        }
                        result
                    }
                };
                replies.push((env.reply, result));
            }

            if wal_error.is_none() {
                if let Some(wal) = self.wal.as_mut() {
                    if let Err(err) = wal.commit() {
                        wal_error = Some(err.to_string());
                    }
                }
            }

            if let Some(msg) = wal_error {
                // Fail-stop: in-memory state is now ahead of the durable log,
                // so nothing from this batch is acknowledged or published.
                for (reply, _) in replies {
                    let _ = reply.send(Err(Error::Wal(msg.clone())));
                }
                return;
            }

            if self.graph.pending_delta() >= self.graph.config().compact_threshold {
                self.graph.compact();
            }
            let snapshot = Arc::new(self.graph.clone());
            *self.published.write().unwrap_or_else(|e| e.into_inner()) = snapshot;

            for (reply, result) in replies {
                let _ = reply.send(result);
            }
            if stop {
                return;
            }
        }
    }
}

/// Applies one command. `enforce = false` replays already-accepted WAL
/// entries without re-running governance.
pub(crate) fn apply(graph: &mut Graph, command: &Command, enforce: bool) -> Result<Reply> {
    match command {
        Command::UpsertNode(input) => graph.upsert_node(input).map(Reply::Node),
        Command::RemoveNode(key) => Ok(Reply::Removed(usize::from(graph.remove_node(key)))),
        Command::UpsertEdge(input) => graph.put_edge(input, enforce).map(Reply::Edge),
        Command::RemoveEdge { src, dst, relation } => {
            Ok(Reply::Removed(graph.remove_edge(src, dst, relation.as_deref())))
        }
        Command::Supersede { old, new, at } => graph.supersede(old, new, *at).map(|_| Reply::Done),
        Command::RegisterRelation { name, layer, category, description } => graph
            .register_relation(name, *layer, *category, description)
            .map(Reply::Relation),
        Command::Compact => {
            graph.compact();
            Ok(Reply::Done)
        }
    }
}
