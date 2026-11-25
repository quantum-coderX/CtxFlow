//! Error type shared by the graph, WAL and engine.

use std::fmt;

use crate::model::Layer;

#[derive(Debug)]
pub enum Error {
    UnknownNode(String),
    InvalidInput(String),
    DegreeCapExceeded { node: String, cap: u32 },
    ConfidenceTooLow { relation: String, confidence: f32, required: f32 },
    LayerChange { relation: String, from: Layer, to: Layer },
    SymbolOverflow(&'static str),
    Io(std::io::Error),
    Corrupt(String),
    Wal(String),
    EngineStopped,
}

pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnknownNode(key) => write!(f, "unknown node {key:?}"),
            Error::InvalidInput(msg) => write!(f, "invalid input: {msg}"),
            Error::DegreeCapExceeded { node, cap } => {
                write!(f, "node {node:?} reached its out-degree cap of {cap}")
            }
            Error::ConfidenceTooLow { relation, confidence, required } => write!(
                f,
                "inferred {relation:?} edge has confidence {confidence}, below required {required}"
            ),
            Error::LayerChange { relation, from, to } => write!(
                f,
                "relation {relation:?} is stored in the {} layer and cannot move to {}",
                from.as_str(),
                to.as_str()
            ),
            Error::SymbolOverflow(what) => write!(f, "too many distinct {what} symbols"),
            Error::Io(err) => write!(f, "io error: {err}"),
            Error::Corrupt(msg) => write!(f, "corrupt data: {msg}"),
            Error::Wal(msg) => write!(f, "write-ahead log failure: {msg}"),
            Error::EngineStopped => write!(f, "memory engine has stopped"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(err) => Some(err),
            _ => None,
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(err: std::io::Error) -> Self {
        Error::Io(err)
    }
}
