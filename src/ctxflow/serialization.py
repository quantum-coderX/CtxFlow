"""Serialization — export/import the graph for persistence across sessions.

Supports two formats:
- **JSON** (default): human-readable, zero extra dependencies.
- **MessagePack** (optional): binary, faster for large graphs. Requires
  ``pip install ctxflow[binary]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ctxflow.graph import ContextGraph
from ctxflow.models import Edge, Node


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _node_to_dict(node: Node) -> Dict[str, Any]:
    """Serialize a Node to a JSON-compatible dict."""
    return {
        "id": node.id,
        "content": node.content,
        "summary": node.summary,
        "embedding": node.embedding,
        "tags": sorted(node.tags),  # Sorted for deterministic output.
        "node_type": node.node_type,
        "timestamp": node.timestamp,
        "metadata": node.metadata,
        "last_accessed": node.last_accessed,
        "access_count": node.access_count,
        "status": node.status,
        "superseded_by": node.superseded_by,
    }


def _dict_to_node(d: Dict[str, Any]) -> Node:
    """Deserialize a dict to a Node."""
    return Node(
        id=d["id"],
        content=d["content"],
        summary=d.get("summary", ""),
        embedding=d.get("embedding"),
        tags=set(d.get("tags", [])),
        node_type=d.get("node_type", "generic"),
        timestamp=d.get("timestamp", 0.0),
        metadata=d.get("metadata", {}),
        last_accessed=d.get("last_accessed", 0.0),
        access_count=d.get("access_count", 0),
        status=d.get("status", "active"),
        superseded_by=d.get("superseded_by"),
    )


def _edge_to_dict(edge: Edge) -> Dict[str, Any]:
    """Serialize an Edge to a JSON-compatible dict."""
    return {
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "relation_type": edge.relation_type,
        "weight": edge.weight,
        "timestamp": edge.timestamp,
        "metadata": edge.metadata,
        "confidence": edge.confidence,
        "created_by": edge.created_by,
    }


def _dict_to_edge(d: Dict[str, Any]) -> Edge:
    """Deserialize a dict to an Edge."""
    return Edge(
        source_id=d["source_id"],
        target_id=d["target_id"],
        relation_type=d.get("relation_type", "related_to"),
        weight=d.get("weight", 1.0),
        timestamp=d.get("timestamp", 0.0),
        metadata=d.get("metadata", {}),
        confidence=d.get("confidence", 1.0),
        created_by=d.get("created_by", "rule"),
    )


def _graph_to_payload(graph: ContextGraph) -> Dict[str, Any]:
    """Convert a ContextGraph to a serializable payload."""
    nodes = graph.all_nodes()
    edges: List[Edge] = []
    for node in nodes:
        edges.extend(graph.get_edges_from(node.id))

    return {
        "version": 1,
        "nodes": [_node_to_dict(n) for n in nodes],
        "edges": [_edge_to_dict(e) for e in edges],
    }


def _payload_to_graph(payload: Dict[str, Any]) -> ContextGraph:
    """Reconstruct a ContextGraph from a deserialized payload."""
    graph = ContextGraph()
    for nd in payload.get("nodes", []):
        graph.add_node(_dict_to_node(nd))
    for ed in payload.get("edges", []):
        graph.add_edge(_dict_to_edge(ed))
    return graph


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def export_json(graph: ContextGraph, path: str) -> None:
    """Serialize *graph* to a JSON file at *path*."""
    payload = _graph_to_payload(graph)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def import_json(path: str) -> ContextGraph:
    """Deserialize a ContextGraph from a JSON file at *path*."""
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text)
    return _payload_to_graph(payload)


# ---------------------------------------------------------------------------
# MessagePack (optional)
# ---------------------------------------------------------------------------

def export_binary(graph: ContextGraph, path: str) -> None:
    """Serialize *graph* to a MessagePack binary file at *path*.

    Raises ``ImportError`` if ``msgpack`` is not installed.
    """
    try:
        import msgpack
    except ImportError as exc:
        raise ImportError(
            "msgpack is required for binary serialization. "
            "Install it with: pip install ctxflow[binary]"
        ) from exc

    payload = _graph_to_payload(graph)
    data = msgpack.packb(payload, use_bin_type=True)
    Path(path).write_bytes(data)


def import_binary(path: str) -> ContextGraph:
    """Deserialize a ContextGraph from a MessagePack file at *path*.

    Raises ``ImportError`` if ``msgpack`` is not installed.
    """
    try:
        import msgpack
    except ImportError as exc:
        raise ImportError(
            "msgpack is required for binary serialization. "
            "Install it with: pip install ctxflow[binary]"
        ) from exc

    data = Path(path).read_bytes()
    payload = msgpack.unpackb(data, raw=False)
    return _payload_to_graph(payload)
