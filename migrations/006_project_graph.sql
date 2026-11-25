-- Project graph storage (nodes and edges of the ResearchOS/CtxFlow research graph).
-- Additive and safe to apply more than once. Independent of the memories tables.

CREATE TABLE IF NOT EXISTS project_graphs (
    project_id TEXT PRIMARY KEY,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_graph_nodes (
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    data JSONB NOT NULL,
    PRIMARY KEY (project_id, node_id)
);

CREATE TABLE IF NOT EXISTS project_graph_edges (
    project_id TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    relation TEXT NOT NULL,
    data JSONB NOT NULL,
    PRIMARY KEY (project_id, src, dst, relation),
    FOREIGN KEY (project_id, src) REFERENCES project_graph_nodes (project_id, node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, dst) REFERENCES project_graph_nodes (project_id, node_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS project_graph_edges_dst_idx
    ON project_graph_edges (project_id, dst);
