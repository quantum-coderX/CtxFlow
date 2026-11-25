"""PostgreSQL storage for whole project graphs, loaded and saved page by page."""

from __future__ import annotations

import json
from typing import Any

from ctxflow.graph import ContextGraph
from ctxflow.serialization import (
    _dict_to_edge,
    _dict_to_node,
    _edge_to_dict,
    _node_to_dict,
)


def _payload(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, (str, bytes)) else dict(value)


class PostgresGraphStore:
    """Async store over ``project_graphs``/``project_graph_nodes``/``project_graph_edges``."""

    def __init__(self, pool: Any, *, page_size: int = 500) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.pool = pool
        self.page_size = page_size

    async def save_graph(self, project_id: str, graph: ContextGraph) -> None:
        """Replace the stored graph atomically."""
        nodes = graph.all_nodes()
        node_rows = [
            (project_id, node.id, json.dumps(_node_to_dict(node))) for node in nodes
        ]
        edge_rows = [
            (
                project_id,
                edge.source_id,
                edge.target_id,
                edge.relation_type,
                json.dumps(_edge_to_dict(edge)),
            )
            for node in nodes
            for edge in graph.get_edges_from(node.id)
        ]
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM project_graph_edges WHERE project_id=$1", project_id
                )
                await connection.execute(
                    "DELETE FROM project_graph_nodes "
                    "WHERE project_id=$1 AND NOT (node_id = ANY($2::text[]))",
                    project_id, [node.id for node in nodes],
                )
                await connection.executemany(
                    """INSERT INTO project_graph_nodes (project_id, node_id, data)
                    VALUES ($1, $2, $3::jsonb)
                    ON CONFLICT (project_id, node_id) DO UPDATE SET data = EXCLUDED.data""",
                    node_rows,
                )
                await connection.executemany(
                    """INSERT INTO project_graph_edges (project_id, src, dst, relation, data)
                    VALUES ($1, $2, $3, $4, $5::jsonb)""",
                    edge_rows,
                )
                await connection.execute(
                    """INSERT INTO project_graphs (project_id, updated_at)
                    VALUES ($1, NOW())
                    ON CONFLICT (project_id) DO UPDATE SET updated_at = NOW()""",
                    project_id,
                )

    async def load_graph(self, project_id: str) -> ContextGraph | None:
        """Rebuild a graph from keyset pages; ``None`` if it was never saved."""
        async with self.pool.acquire() as connection:
            exists = await connection.fetchrow(
                "SELECT 1 FROM project_graphs WHERE project_id=$1", project_id
            )
            if exists is None:
                return None
            graph = ContextGraph()
            cursor: str | None = None
            while True:
                rows = await connection.fetch(
                    """SELECT node_id, data FROM project_graph_nodes
                    WHERE project_id=$1 AND ($2::text IS NULL OR node_id > $2)
                    ORDER BY node_id LIMIT $3""",
                    project_id, cursor, self.page_size,
                )
                for row in rows:
                    graph.add_node(_dict_to_node(_payload(row["data"])))
                if len(rows) < self.page_size:
                    break
                cursor = rows[-1]["node_id"]

            last: tuple[str, str, str] | None = None
            while True:
                rows = await connection.fetch(
                    """SELECT src, dst, relation, data FROM project_graph_edges
                    WHERE project_id=$1
                      AND ($2::text IS NULL
                           OR (src, dst, relation) > ($2::text, $3::text, $4::text))
                    ORDER BY src, dst, relation LIMIT $5""",
                    project_id, *(last or (None, None, None)), self.page_size,
                )
                for row in rows:
                    graph.add_edge(_dict_to_edge(_payload(row["data"])))
                if len(rows) < self.page_size:
                    break
                tail = rows[-1]
                last = (tail["src"], tail["dst"], tail["relation"])
            return graph
