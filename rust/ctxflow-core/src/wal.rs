//! Append-only write-ahead log.
//!
//! File layout: an 8-byte magic header followed by records of
//! `[len: u32 LE][crc32: u32 LE][payload]`. Records hold node keys and
//! relation names rather than interned ids, so replay does not depend on
//! interning order. A torn or corrupt tail is truncated on open.

use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
use std::path::Path;

use crate::engine::Command;
use crate::error::{Error, Result};
use crate::model::{Category, EdgeInput, Layer, NodeInput};

const MAGIC: &[u8; 8] = b"CTXWAL01";

const TAG_UPSERT_NODE: u8 = 1;
const TAG_REMOVE_NODE: u8 = 2;
const TAG_UPSERT_EDGE: u8 = 3;
const TAG_REMOVE_EDGE: u8 = 4;
const TAG_SUPERSEDE: u8 = 5;
const TAG_REGISTER_RELATION: u8 = 6;
const NO_CATEGORY: u8 = u8::MAX;

pub struct Wal {
    out: BufWriter<File>,
    sync: bool,
}

impl Wal {
    /// Opens (or creates) a log and returns it with every intact command,
    /// in order. `sync` makes each commit call `fsync`.
    pub fn open(path: &Path, sync: bool) -> Result<(Wal, Vec<Command>)> {
        let mut file = OpenOptions::new().read(true).write(true).create(true).open(path)?;
        let mut buf = Vec::new();
        file.read_to_end(&mut buf)?;

        let mut commands = Vec::new();
        let good_len = if buf.len() < MAGIC.len() {
            if !MAGIC.starts_with(&buf) {
                return Err(Error::Corrupt(format!("{} is not a ctxflow WAL", path.display())));
            }
            // Empty file or a torn header write: start over.
            file.set_len(0)?;
            file.seek(SeekFrom::Start(0))?;
            file.write_all(MAGIC)?;
            file.sync_data()?;
            MAGIC.len()
        } else {
            if &buf[..MAGIC.len()] != MAGIC {
                return Err(Error::Corrupt(format!("{} is not a ctxflow WAL", path.display())));
            }
            let mut pos = MAGIC.len();
            while let Some((command, next)) = read_record(&buf, pos) {
                commands.push(command);
                pos = next;
            }
            if pos < buf.len() {
                file.set_len(pos as u64)?;
                file.sync_data()?;
            }
            pos
        };

        file.seek(SeekFrom::Start(good_len as u64))?;
        Ok((Wal { out: BufWriter::new(file), sync }, commands))
    }

    /// Buffers one command. Call [`Wal::commit`] to make it durable.
    pub fn append(&mut self, command: &Command) -> Result<()> {
        let Some(payload) = encode(command) else {
            return Ok(());
        };
        let len = u32::try_from(payload.len())
            .map_err(|_| Error::InvalidInput("WAL record exceeds 4 GiB".into()))?;
        self.out.write_all(&len.to_le_bytes())?;
        self.out.write_all(&crc32(&payload).to_le_bytes())?;
        self.out.write_all(&payload)?;
        Ok(())
    }

    pub fn commit(&mut self) -> Result<()> {
        self.out.flush()?;
        if self.sync {
            self.out.get_ref().sync_data()?;
        }
        Ok(())
    }
}

fn read_record(buf: &[u8], pos: usize) -> Option<(Command, usize)> {
    let header = buf.get(pos..pos.checked_add(8)?)?;
    let len = u32::from_le_bytes([header[0], header[1], header[2], header[3]]) as usize;
    let crc = u32::from_le_bytes([header[4], header[5], header[6], header[7]]);
    let start = pos + 8;
    let end = start.checked_add(len)?;
    let payload = buf.get(start..end)?;
    if crc32(payload) != crc {
        return None;
    }
    Some((decode(payload)?, end))
}

// -- Encoding ---------------------------------------------------------------

struct Enc(Vec<u8>);

impl Enc {
    fn u8(&mut self, v: u8) -> &mut Self {
        self.0.push(v);
        self
    }
    fn u32(&mut self, v: u32) -> &mut Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn i64(&mut self, v: i64) -> &mut Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn f32(&mut self, v: f32) -> &mut Self {
        self.u32(v.to_bits())
    }
    fn str(&mut self, s: &str) -> &mut Self {
        self.u32(s.len() as u32);
        self.0.extend_from_slice(s.as_bytes());
        self
    }
}

/// Returns `None` for commands that are not logged (e.g. `Compact`).
pub(crate) fn encode(command: &Command) -> Option<Vec<u8>> {
    let mut e = Enc(Vec::with_capacity(64));
    match command {
        Command::UpsertNode(n) => {
            e.u8(TAG_UPSERT_NODE)
                .str(&n.key)
                .str(&n.node_type)
                .i64(n.created_at)
                .i64(n.valid_from)
                .i64(n.valid_to)
                .f32(n.importance);
        }
        Command::RemoveNode(key) => {
            e.u8(TAG_REMOVE_NODE).str(key);
        }
        Command::UpsertEdge(x) => {
            e.u8(TAG_UPSERT_EDGE)
                .str(&x.src)
                .str(&x.dst)
                .str(&x.relation)
                .str(&x.origin)
                .f32(x.weight)
                .f32(x.confidence)
                .i64(x.valid_from)
                .i64(x.valid_to);
        }
        Command::RemoveEdge { src, dst, relation } => {
            e.u8(TAG_REMOVE_EDGE).str(src).str(dst);
            match relation {
                Some(name) => e.u8(1).str(name),
                None => e.u8(0),
            };
        }
        Command::Supersede { old, new, at } => {
            e.u8(TAG_SUPERSEDE).str(old).str(new).i64(*at);
        }
        Command::RegisterRelation { name, layer, category, description } => {
            e.u8(TAG_REGISTER_RELATION)
                .str(name)
                .u8(*layer as u8)
                .u8(category.map_or(NO_CATEGORY, |c| c as u8))
                .str(description);
        }
        Command::Compact => return None,
    }
    Some(e.0)
}

struct Dec<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Dec<'a> {
    fn take(&mut self, n: usize) -> Option<&'a [u8]> {
        let end = self.pos.checked_add(n)?;
        let bytes = self.buf.get(self.pos..end)?;
        self.pos = end;
        Some(bytes)
    }
    fn u8(&mut self) -> Option<u8> {
        Some(self.take(1)?[0])
    }
    fn u32(&mut self) -> Option<u32> {
        Some(u32::from_le_bytes(self.take(4)?.try_into().ok()?))
    }
    fn i64(&mut self) -> Option<i64> {
        Some(i64::from_le_bytes(self.take(8)?.try_into().ok()?))
    }
    fn f32(&mut self) -> Option<f32> {
        Some(f32::from_bits(self.u32()?))
    }
    fn str(&mut self) -> Option<String> {
        let len = self.u32()? as usize;
        String::from_utf8(self.take(len)?.to_vec()).ok()
    }
}

pub(crate) fn decode(payload: &[u8]) -> Option<Command> {
    let mut d = Dec { buf: payload, pos: 0 };
    let command = match d.u8()? {
        TAG_UPSERT_NODE => Command::UpsertNode(NodeInput {
            key: d.str()?,
            node_type: d.str()?,
            created_at: d.i64()?,
            valid_from: d.i64()?,
            valid_to: d.i64()?,
            importance: d.f32()?,
        }),
        TAG_REMOVE_NODE => Command::RemoveNode(d.str()?),
        TAG_UPSERT_EDGE => Command::UpsertEdge(EdgeInput {
            src: d.str()?,
            dst: d.str()?,
            relation: d.str()?,
            origin: d.str()?,
            weight: d.f32()?,
            confidence: d.f32()?,
            valid_from: d.i64()?,
            valid_to: d.i64()?,
        }),
        TAG_REMOVE_EDGE => {
            let src = d.str()?;
            let dst = d.str()?;
            let relation = match d.u8()? {
                0 => None,
                1 => Some(d.str()?),
                _ => return None,
            };
            Command::RemoveEdge { src, dst, relation }
        }
        TAG_SUPERSEDE => Command::Supersede { old: d.str()?, new: d.str()?, at: d.i64()? },
        TAG_REGISTER_RELATION => {
            let name = d.str()?;
            let layer = Layer::from_u8(d.u8()?)?;
            let category = match d.u8()? {
                NO_CATEGORY => None,
                v => Some(Category::from_u8(v)?),
            };
            Command::RegisterRelation { name, layer, category, description: d.str()? }
        }
        _ => return None,
    };
    (d.pos == payload.len()).then_some(command)
}

// -- CRC-32 (IEEE) ----------------------------------------------------------

const fn crc_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            k += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
}

const CRC_TABLE: [u32; 256] = crc_table();

pub(crate) fn crc32(data: &[u8]) -> u32 {
    let mut c = !0u32;
    for &b in data {
        c = CRC_TABLE[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    !c
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_matches_reference_vector() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn every_logged_command_round_trips() {
        let commands = vec![
            Command::UpsertNode(NodeInput::new("a").with_type("claim").at(7).valid_until(9)),
            Command::RemoveNode("a".into()),
            Command::UpsertEdge(
                EdgeInput::new("a", "b", "supports").with_confidence(0.7).with_origin("laya").valid(1, 2),
            ),
            Command::RemoveEdge { src: "a".into(), dst: "b".into(), relation: None },
            Command::RemoveEdge { src: "a".into(), dst: "b".into(), relation: Some("cites".into()) },
            Command::Supersede { old: "a".into(), new: "b".into(), at: 42 },
            Command::RegisterRelation {
                name: "replicates".into(),
                layer: Layer::Causal,
                category: Some(Category::Research),
                description: "d".into(),
            },
            Command::RegisterRelation {
                name: "near".into(),
                layer: Layer::Entity,
                category: None,
                description: String::new(),
            },
        ];
        for command in commands {
            let payload = encode(&command).unwrap();
            assert_eq!(decode(&payload), Some(command));
        }
        assert!(encode(&Command::Compact).is_none());
    }

    #[test]
    fn trailing_bytes_are_rejected() {
        let mut payload = encode(&Command::RemoveNode("a".into())).unwrap();
        payload.push(0);
        assert_eq!(decode(&payload), None);
    }
}
