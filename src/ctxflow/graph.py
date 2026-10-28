"""Core graph engine — schema-agnostic, in-memory, backed by NetworkX.

ContextGraph owns the underlying nx.DiGraph and provides O(1) node/edge
insertion, BFS traversal, and tag-based candidate retrieval. It does NOT
implement scoring or governance — those are separate modules that operate
on the graph.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from ctxflow.models import Edge, Node, NodeStatus, ProvenanceChain, ProvenanceStep


class ContextGraph:
    """In-memory directed graph wrapping ``networkx.DiGraph``.

    Nodes are stored as graph-node attributes keyed by ``node.id``.
    Edges carry ``relation_type``, ``weight``, ``timestamp``, and
    ``metadata`` as edge-data attributes.
    """

    def __init__(self) -> None:
        self._g: nx.DiGraph = nx.DiGraph()
        # Fast lookup: tag → set of node IDs that have this tag.
        self._tag_index: Dict[str, Set[str]] = {}

    # -- Properties ---------------------------------------------------------

    @property
    def node_count(self) -> int:
        return self._g.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._g.number_of_edges()

    # -- Node operations ----------------------------------------------------

    def add_node(self, node: Node) -> None:
        """Insert a node in O(1). Overwrites if *node.id* already exists."""
        self._g.add_node(node.id, data=node)
        # Update tag index.
        for tag in node.tags:
            self._tag_index.setdefault(tag, set()).add(node.id)

    def get_node(self, node_id: str) -> Optional[Node]:
        """Return the Node with *node_id*, or ``None``."""
        if node_id not in self._g:
            return None
        return self._g.nodes[node_id]["data"]

    def has_node(self, node_id: str) -> bool:
        return node_id in self._g

    def remove_node(self, node_id: str) -> Optional[Node]:
        """Remove a node and all its incident edges. Returns the removed Node."""
        node = self.get_node(node_id)
        if node is None:
            return None
        # Clean tag index.
        for tag in node.tags:
            bucket = self._tag_index.get(tag)
            if bucket:
                bucket.discard(node_id)
                if not bucket:
                    del self._tag_index[tag]
        self._g.remove_node(node_id)
        return node

    def all_nodes(self) -> List[Node]:
        """Return all nodes (order is insertion-dependent)."""
        return [self._g.nodes[nid]["data"] for nid in self._g.nodes]

    def get_nodes_by_status(self, status: str) -> List[Node]:
        """Return all nodes with the given status.

        Parameters
        ----------
        status : str
            One of ``"active"``, ``"superseded"``, ``"contested"``.
        """
        return [n for n in self.all_nodes() if n.status == status]

    # -- Edge operations ----------------------------------------------------

    def _edge_from_data(
        self, source_id: str, target_id: str, data: dict
    ) -> Edge:
        """Reconstruct an Edge from NetworkX edge-data dict."""
        return Edge(
            source_id=source_id,
            target_id=target_id,
            relation_type=data.get("relation_type", "related_to"),
            weight=data.get("weight", 1.0),
            timestamp=data.get("timestamp", 0.0),
            metadata=data.get("metadata", {}),
        )

    def add_edge(self, edge: Edge) -> bool:
        """Insert a directed edge. Returns ``False`` if source or target is missing."""
        if not self.has_node(edge.source_id) or not self.has_node(edge.target_id):
            return False
        self._g.add_edge(
            edge.source_id,
            edge.target_id,
            relation_type=edge.relation_type,
            weight=edge.weight,
            timestamp=edge.timestamp,
            metadata=edge.metadata,
        )
        return True

    def remove_edge(self, source_id: str, target_id: str) -> Optional[Edge]:
        """Remove one directed edge and return its previous value."""

        if not self._g.has_edge(source_id, target_id):
            return None
        data = dict(self._g.get_edge_data(source_id, target_id) or {})
        edge = self._edge_from_data(source_id, target_id, data)
        self._g.remove_edge(source_id, target_id)
        return edge

    def get_edges_from(self, node_id: str) -> List[Edge]:
        """Return all outgoing edges from *node_id*."""
        if node_id not in self._g:
            return []
        return [
            self._edge_from_data(node_id, target, data)
            for _, target, data in self._g.out_edges(node_id, data=True)
        ]

    def get_edges_to(self, node_id: str) -> List[Edge]:
        """Return all incoming edges to *node_id*."""
        if node_id not in self._g:
            return []
        return [
            self._edge_from_data(source, node_id, data)
            for source, _, data in self._g.in_edges(node_id, data=True)
        ]

    def get_edges_by_type(
        self, node_id: str, relation_type: str, *, direction: str = "both"
    ) -> List[Edge]:
        """Return edges incident to *node_id* with a specific relation type.

        Parameters
        ----------
        node_id : str
            The node to inspect.
        relation_type : str
            The relation type to filter by (e.g. ``"supersedes"``).
        direction : str
            ``"out"`` for outgoing only, ``"in"`` for incoming only,
            ``"both"`` for both directions.
        """
        edges: List[Edge] = []
        if direction in ("out", "both"):
            edges.extend(
                e for e in self.get_edges_from(node_id)
                if e.relation_type == relation_type
            )
        if direction in ("in", "both"):
            edges.extend(
                e for e in self.get_edges_to(node_id)
                if e.relation_type == relation_type
            )
        return edges

    def out_degree(self, node_id: str) -> int:
        """Number of outgoing edges from *node_id*."""
        if node_id not in self._g:
            return 0
        return self._g.out_degree(node_id)

    def in_degree(self, node_id: str) -> int:
        """Number of incoming edges to *node_id*."""
        if node_id not in self._g:
            return 0
        return self._g.in_degree(node_id)

    def total_degree(self, node_id: str) -> int:
        """Sum of in-degree and out-degree."""
        return self.in_degree(node_id) + self.out_degree(node_id)

    # -- Traversal ----------------------------------------------------------

    def bfs_distances(
        self, start_id: str, max_depth: int = 3
    ) -> Dict[str, int]:
        """Bounded BFS from *start_id*.

        Returns ``{node_id: hop_distance}`` for all reachable nodes within
        *max_depth* hops (excluding the start node itself). Traverses both
        outgoing and incoming edges (undirected BFS over the directed graph).
        """
        if start_id not in self._g:
            return {}

        visited: Dict[str, int] = {}
        queue: deque[Tuple[str, int]] = deque([(start_id, 0)])
        seen: Set[str] = {start_id}

        while queue:
            current, depth = queue.popleft()
            if depth > 0:
                visited[current] = depth
            if depth >= max_depth:
                continue
            # Successors + predecessors (treat as undirected for proximity).
            neighbors = set(self._g.successors(current)) | set(
                self._g.predecessors(current)
            )
            for nbr in neighbors:
                if nbr not in seen:
                    seen.add(nbr)
                    queue.append((nbr, depth + 1))

        return visited

    # -- Provenance traversal -----------------------------------------------

    # Edge types that represent provenance (backward-walkable).
    _PROVENANCE_EDGE_TYPES: Set[str] = {
        "follows", "derived_from", "extracted_from", "references",
    }
    # Edge types that represent contradiction / supersession.
    _CONTRADICTION_EDGE_TYPES: Set[str] = {
        "supersedes", "contradicts",
    }

    def trace_provenance(
        self,
        node_id: str,
        max_depth: int = 10,
    ) -> ProvenanceChain:
        """Walk backward through provenance edges from *node_id*.

        Follows ``follows``, ``derived_from``, ``extracted_from``, and
        ``references`` edges backward (i.e. from target to source) to
        build a chain from the given node to its original sources.

        Also collects any ``supersedes`` or ``contradicts`` edges that
        touch nodes in the chain.

        Parameters
        ----------
        node_id : str
            The starting node (typically a claim or answer node).
        max_depth : int
            Maximum backward hops to follow.

        Returns
        -------
        ProvenanceChain
            The root node, ordered provenance steps, and any
            contradiction edges found along the chain.
        """
        root = self.get_node(node_id)
        if root is None:
            return ProvenanceChain(
                root=Node(content="", id=node_id),
                steps=[],
                contradictions=[],
            )

        steps: List[ProvenanceStep] = [ProvenanceStep(node=root, edge=None)]
        contradictions: List[Tuple[Node, Edge]] = []
        visited: Set[str] = {node_id}

        # BFS backward through provenance edges.
        queue: deque[Tuple[str, int]] = deque([(node_id, 0)])

        while queue:
            current_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            # Check incoming edges (we walk backward: target -> source).
            for edge in self.get_edges_to(current_id):
                if edge.relation_type in self._PROVENANCE_EDGE_TYPES:
                    if edge.source_id not in visited:
                        visited.add(edge.source_id)
                        source_node = self.get_node(edge.source_id)
                        if source_node is not None:
                            steps.append(
                                ProvenanceStep(node=source_node, edge=edge)
                            )
                            queue.append((edge.source_id, depth + 1))

            # Also check for contradiction edges touching this node.
            for edge in self.get_edges_to(current_id):
                if edge.relation_type in self._CONTRADICTION_EDGE_TYPES:
                    contra_node = self.get_node(edge.source_id)
                    if contra_node is not None:
                        contradictions.append((contra_node, edge))

            for edge in self.get_edges_from(current_id):
                if edge.relation_type in self._CONTRADICTION_EDGE_TYPES:
                    contra_node = self.get_node(edge.target_id)
                    if contra_node is not None:
                        contradictions.append((contra_node, edge))

        return ProvenanceChain(
            root=root,
            steps=steps,
            contradictions=contradictions,
        )

    # -- Tag-based retrieval ------------------------------------------------

    @staticmethod
    def jaccard(a: Set[str], b: Set[str]) -> float:
        """Jaccard similarity between two tag sets."""
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0

    def find_similar_by_tags(
        self, tags: Set[str], k: int = 5, min_similarity: float = 0.0,
        exclude: Set[str] | None = None,
    ) -> List[Tuple[Node, float]]:
        """Find the top-*k* nodes most similar to *tags* by Jaccard overlap.

        Uses the tag index for a fast pre-filter: only nodes sharing at least
        one tag are scored.
        """
        if not tags:
            return []

        exclude = exclude or set()

        # Candidate set: any node sharing at least one tag.
        candidate_ids: Set[str] = set()
        for tag in tags:
            bucket = self._tag_index.get(tag)
            if bucket:
                candidate_ids.update(bucket)
        candidate_ids -= exclude

        scored: List[Tuple[Node, float]] = []
        for nid in candidate_ids:
            node = self.get_node(nid)
            if node is None:
                continue
            sim = self.jaccard(tags, node.tags)
            if sim >= min_similarity:
                scored.append((node, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    # -- Internals (for serialization) --------------------------------------

    @property
    def _graph(self) -> nx.DiGraph:
        """Direct access to the underlying NetworkX graph (for serialization)."""
        return self._g
