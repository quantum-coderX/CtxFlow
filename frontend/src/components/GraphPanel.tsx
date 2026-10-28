import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import { api } from "../api";
import type { GraphData, GraphNode } from "../types";
import { useApp } from "../store/AppContext";

interface Point {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

interface DragState {
  pointerId: number;
  nodeId: string | null;
  lastX: number;
  lastY: number;
  moved: boolean;
}

const NODE_COLORS: Record<string, string> = {
  paper: "#68a7ff",
  claim: "#a78bfa",
  method: "#42d3b5",
  result: "#f5b84b",
  finding: "#fb7185",
  hypothesis: "#fb7185",
  note: "#f59e0b",
  answer: "#4ade80",
  source: "#38bdf8",
  generic: "#8794a8",
};

const SUPERSEDED_COLOR = "#ef6475";
const EDGE_COLOR = "rgba(142, 157, 181, 0.18)";
const SELECTED_COLOR = "#c4b5fd";
const REFRESH_INTERVAL_MS = 2500;

function hashString(value: string): number {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function nodeColor(node: GraphNode): string {
  if (node.status === "superseded") return SUPERSEDED_COLOR;
  return NODE_COLORS[node.node_type] ?? NODE_COLORS.generic;
}

function layoutGraph(data: GraphData, previous: Map<string, Point>): Map<string, Point> {
  const nodes = data.nodes;
  if (!nodes.length) return new Map();

  const worldWidth = Math.max(1400, Math.sqrt(nodes.length) * 70);
  const worldHeight = Math.max(900, worldWidth * 0.68);
  const groups = new Map<string, GraphNode[]>();
  for (const node of nodes) {
    const group = groups.get(node.node_type) ?? [];
    group.push(node);
    groups.set(node.node_type, group);
  }

  const types = [...groups.keys()].sort();
  const positions = new Map<string, Point>();
  const indexById = new Map<string, number>();
  nodes.forEach((node, index) => indexById.set(node.id, index));
  const neighbors = new Map<string, string[]>();
  for (const edge of data.edges) {
    if (!indexById.has(edge.source_id) || !indexById.has(edge.target_id)) continue;
    const sourceNeighbors = neighbors.get(edge.source_id) ?? [];
    sourceNeighbors.push(edge.target_id);
    neighbors.set(edge.source_id, sourceNeighbors);
    const targetNeighbors = neighbors.get(edge.target_id) ?? [];
    targetNeighbors.push(edge.source_id);
    neighbors.set(edge.target_id, targetNeighbors);
  }

  types.forEach((type, typeIndex) => {
    const group = groups.get(type) ?? [];
    const angle = (typeIndex / Math.max(types.length, 1)) * Math.PI * 2 - Math.PI / 2;
    const clusterX = worldWidth / 2 + Math.cos(angle) * worldWidth * 0.24;
    const clusterY = worldHeight / 2 + Math.sin(angle) * worldHeight * 0.24;
    const radius = Math.max(54, Math.sqrt(group.length) * 24);

    group.forEach((node, groupIndex) => {
      const old = previous.get(node.id);
      if (old) {
        positions.set(node.id, { ...old, vx: 0, vy: 0 });
        return;
      }

      const connected = (neighbors.get(node.id) ?? [])
        .map((id) => previous.get(id))
        .filter((point): point is Point => point !== undefined);
      if (connected.length) {
        const center = connected.reduce(
          (sum, point) => ({ x: sum.x + point.x, y: sum.y + point.y }),
          { x: 0, y: 0 }
        );
        const seed = hashString(node.id);
        const jitterAngle = (seed / 0xffffffff) * Math.PI * 2;
        const jitterRadius = 24 + ((seed >>> 8) % 42);
        positions.set(node.id, {
          x: center.x / connected.length + Math.cos(jitterAngle) * jitterRadius,
          y: center.y / connected.length + Math.sin(jitterAngle) * jitterRadius,
          vx: 0,
          vy: 0,
        });
        return;
      }

      const seed = hashString(node.id);
      const angleOffset = ((seed % 10000) / 10000) * Math.PI * 2;
      const distance = radius * Math.sqrt((groupIndex + 0.5) / Math.max(group.length, 1));
      const angleAtNode = groupIndex * 2.399963 + angleOffset;
      positions.set(node.id, {
        x: clusterX + Math.cos(angleAtNode) * distance,
        y: clusterY + Math.sin(angleAtNode) * distance,
        vx: 0,
        vy: 0,
      });
    });
  });

  const iterations = previous.size ? 12 : nodes.length > 1800 ? 24 : 38;
  const cellSize = 100;
  const maximumSpeed = 11;
  for (let iteration = 0; iteration < iterations; iteration += 1) {
    const cells = new Map<string, string[]>();
    for (const node of nodes) {
      const point = positions.get(node.id);
      if (!point) continue;
      const key = `${Math.floor(point.x / cellSize)}:${Math.floor(point.y / cellSize)}`;
      const cell = cells.get(key) ?? [];
      cell.push(node.id);
      cells.set(key, cell);
    }

    for (const node of nodes) {
      const point = positions.get(node.id);
      if (!point) continue;
      const cellX = Math.floor(point.x / cellSize);
      const cellY = Math.floor(point.y / cellSize);
      for (let offsetX = -1; offsetX <= 1; offsetX += 1) {
        for (let offsetY = -1; offsetY <= 1; offsetY += 1) {
          const nearby = cells.get(`${cellX + offsetX}:${cellY + offsetY}`) ?? [];
          for (const nearbyId of nearby) {
            if (nearbyId <= node.id) continue;
            const other = positions.get(nearbyId);
            if (!other) continue;
            const dx = point.x - other.x;
            const dy = point.y - other.y;
            const distanceSquared = dx * dx + dy * dy;
            if (distanceSquared > cellSize * cellSize * 2) continue;
            const distance = Math.sqrt(distanceSquared) || 0.1;
            const force = Math.min(1.6, 420 / distanceSquared);
            point.vx += (dx / distance) * force;
            point.vy += (dy / distance) * force;
            other.vx -= (dx / distance) * force;
            other.vy -= (dy / distance) * force;
          }
        }
      }
    }

    for (const edge of data.edges) {
      const source = positions.get(edge.source_id);
      const target = positions.get(edge.target_id);
      if (!source || !target) continue;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.sqrt(dx * dx + dy * dy) || 0.1;
      const force = (distance - 105) * 0.0025;
      source.vx += (dx / distance) * force;
      source.vy += (dy / distance) * force;
      target.vx -= (dx / distance) * force;
      target.vy -= (dy / distance) * force;
    }

    for (const point of positions.values()) {
      point.vx += (worldWidth / 2 - point.x) * 0.0008;
      point.vy += (worldHeight / 2 - point.y) * 0.0008;
      point.vx = Math.max(-maximumSpeed, Math.min(maximumSpeed, point.vx)) * 0.72;
      point.vy = Math.max(-maximumSpeed, Math.min(maximumSpeed, point.vy)) * 0.72;
      point.x += point.vx;
      point.y += point.vy;
    }
  }

  return positions;
}

export function GraphPanel() {
  const { state, dispatch } = useApp();
  const projectId = state.currentProjectId;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const fittedRef = useRef(false);
  const loadedProjectRef = useRef<string | null>(null);
  const [graph, setGraph] = useState<GraphData>({ nodes: [], edges: [] });
  const [positions, setPositions] = useState<Map<string, Point>>(new Map());
  const [viewport, setViewport] = useState<Viewport>({ x: 0, y: 0, zoom: 1 });
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const selectedNodeId = state.traceNodeId;

  const fitGraph = useCallback((points: Map<string, Point>) => {
    if (!points.size || !size.width || !size.height) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const point of points.values()) {
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x);
      maxY = Math.max(maxY, point.y);
    }
    const graphWidth = Math.max(maxX - minX, 100);
    const graphHeight = Math.max(maxY - minY, 100);
    const zoom = Math.max(0.1, Math.min(
      (size.width - 80) / graphWidth,
      (size.height - 80) / graphHeight,
      1.8
    ));
    setViewport({
      zoom,
      x: size.width / 2 - ((minX + maxX) / 2) * zoom,
      y: size.height / 2 - ((minY + maxY) / 2) * zoom,
    });
  }, [size.height, size.width]);

  useEffect(() => {
    fittedRef.current = false;
    loadedProjectRef.current = null;
    setGraph({ nodes: [], edges: [] });
    setPositions(new Map());
    setViewport({ x: 0, y: 0, zoom: 1 });
    setSearch("");
    setTypeFilter("all");
    setLoading(true);
    setError("");
    setLastUpdated(null);
  }, [projectId]);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let lastRevision: number | null = null;

    const refresh = async (force: boolean) => {
      if (inFlight) return;
      inFlight = true;
      try {
        const stats = await api.graph.stats(projectId);
        if (!active) return;
        dispatch({ type: "SET_GRAPH_STATS", payload: stats });
        const changed = force || stats.revision !== lastRevision;
        if (changed) {
          const nextGraph = await api.graph.data(projectId);
          if (!active) return;
          loadedProjectRef.current = projectId;
          setGraph(nextGraph);
          setPositions((previous) => layoutGraph(nextGraph, previous));
          setLastUpdated(Date.now());
        }
        lastRevision = stats.revision;
        setError("");
      } catch (reason: unknown) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Could not load graph data.");
      } finally {
        if (active) {
          setLoading(false);
          inFlight = false;
        }
      }
    };

    void refresh(true);
    const interval = window.setInterval(() => void refresh(false), REFRESH_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [dispatch, projectId, refreshKey]);

  useEffect(() => {
    if (
      fittedRef.current ||
      loadedProjectRef.current !== projectId ||
      !positions.size ||
      !size.width ||
      !size.height
    ) return;
    fitGraph(positions);
    fittedRef.current = true;
  }, [fitGraph, positions, projectId, size.height, size.width]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) setSize({ width: rect.width, height: rect.height });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const visibleNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const node of graph.nodes) {
      if (typeFilter === "all" || node.node_type === typeFilter) ids.add(node.id);
    }
    return ids;
  }, [graph.nodes, typeFilter]);

  const degrees = useMemo(() => {
    const result = new Map<string, number>();
    for (const edge of graph.edges) {
      result.set(edge.source_id, (result.get(edge.source_id) ?? 0) + 1);
      result.set(edge.target_id, (result.get(edge.target_id) ?? 0) + 1);
    }
    return result;
  }, [graph.edges]);

  const neighbors = useMemo(() => {
    const result = new Set<string>();
    if (!selectedNodeId) return result;
    result.add(selectedNodeId);
    for (const edge of graph.edges) {
      if (edge.source_id === selectedNodeId) result.add(edge.target_id);
      if (edge.target_id === selectedNodeId) result.add(edge.source_id);
    }
    return result;
  }, [graph.edges, selectedNodeId]);

  const searchResults = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return [];
    return graph.nodes
      .filter((node) =>
        `${node.id} ${node.node_type} ${node.summary ?? ""} ${node.content}`.toLowerCase().includes(query)
      )
      .slice(0, 8);
  }, [graph.nodes, search]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !size.width || !size.height) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(size.width * ratio);
    canvas.height = Math.round(size.height * ratio);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, size.width, size.height);
    context.fillStyle = "#0b0e14";
    context.fillRect(0, 0, size.width, size.height);
    context.save();
    context.translate(viewport.x, viewport.y);
    context.scale(viewport.zoom, viewport.zoom);

    for (const edge of graph.edges) {
      if (!visibleNodeIds.has(edge.source_id) || !visibleNodeIds.has(edge.target_id)) continue;
      const source = positions.get(edge.source_id);
      const target = positions.get(edge.target_id);
      if (!source || !target) continue;
      const highlighted = selectedNodeId === edge.source_id || selectedNodeId === edge.target_id;
      context.strokeStyle = highlighted ? "rgba(196, 181, 253, 0.8)" : EDGE_COLOR;
      context.lineWidth = highlighted ? 1.6 / viewport.zoom : 0.75 / viewport.zoom;
      context.beginPath();
      context.moveTo(source.x, source.y);
      context.lineTo(target.x, target.y);
      context.stroke();
    }

    for (const node of graph.nodes) {
      if (!visibleNodeIds.has(node.id)) continue;
      const point = positions.get(node.id);
      if (!point) continue;
      const radius = Math.min(8, 3.2 + Math.sqrt(degrees.get(node.id) ?? 0) * 0.65);
      const selected = node.id === selectedNodeId;
      const hovered = node.id === hoveredNodeId;
      context.beginPath();
      context.arc(point.x, point.y, (selected || hovered ? radius + 3 : radius) / viewport.zoom, 0, Math.PI * 2);
      context.fillStyle = nodeColor(node);
      context.globalAlpha = selectedNodeId && !neighbors.has(node.id) ? 0.36 : 0.95;
      context.fill();
      context.globalAlpha = 1;
      if (selected || hovered) {
        context.strokeStyle = SELECTED_COLOR;
        context.lineWidth = 1.8 / viewport.zoom;
        context.stroke();
        const label = (node.summary || node.content || node.node_type).replace(/\s+/g, " ").slice(0, 54);
        context.font = `${12 / viewport.zoom}px Inter, system-ui, sans-serif`;
        context.fillStyle = "#f4f3ff";
        context.textAlign = "left";
        context.fillText(label, point.x + radius / viewport.zoom + 7 / viewport.zoom, point.y + 4 / viewport.zoom);
      }
    }

    context.restore();
  }, [degrees, graph.edges, graph.nodes, hoveredNodeId, neighbors, positions, selectedNodeId, size.height, size.width, viewport, visibleNodeIds]);

  useEffect(() => {
    draw();
  }, [draw]);

  const nodeAt = useCallback((clientX: number, clientY: number): string | null => {
    const bounds = canvasRef.current?.getBoundingClientRect();
    if (!bounds) return null;
    const worldX = (clientX - bounds.left - viewport.x) / viewport.zoom;
    const worldY = (clientY - bounds.top - viewport.y) / viewport.zoom;
    let nearest: string | null = null;
    let nearestDistance = 13 / viewport.zoom;
    for (const node of graph.nodes) {
      if (!visibleNodeIds.has(node.id)) continue;
      const point = positions.get(node.id);
      if (!point) continue;
      const dx = point.x - worldX;
      const dy = point.y - worldY;
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance < nearestDistance) {
        nearest = node.id;
        nearestDistance = distance;
      }
    }
    return nearest;
  }, [graph.nodes, positions, viewport, visibleNodeIds]);

  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      nodeId: nodeAt(event.clientX, event.clientY),
      lastX: event.clientX,
      lastY: event.clientY,
      moved: false,
    };
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) {
      setHoveredNodeId(nodeAt(event.clientX, event.clientY));
      return;
    }
    const dx = event.clientX - drag.lastX;
    const dy = event.clientY - drag.lastY;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    drag.lastX = event.clientX;
    drag.lastY = event.clientY;

    if (drag.nodeId) {
      const point = positions.get(drag.nodeId);
      if (point) {
        point.x += dx / viewport.zoom;
        point.y += dy / viewport.zoom;
        setPositions(new Map(positions));
      }
    } else {
      setViewport((current) => ({ ...current, x: current.x + dx, y: current.y + dy }));
    }
  };

  const onPointerUp = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.nodeId && !drag.moved) dispatch({ type: "SET_TRACE_NODE", payload: drag.nodeId });
    dragRef.current = null;
  };

  const onWheel = (event: ReactWheelEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;
    setViewport((current) => {
      const zoom = Math.max(0.18, Math.min(4, current.zoom * (event.deltaY < 0 ? 1.12 : 0.89)));
      return {
        zoom,
        x: x - ((x - current.x) / current.zoom) * zoom,
        y: y - ((y - current.y) / current.zoom) * zoom,
      };
    });
  };

  const zoomBy = (factor: number) => {
    const centerX = size.width / 2;
    const centerY = size.height / 2;
    setViewport((current) => {
      const zoom = Math.max(0.18, Math.min(4, current.zoom * factor));
      return {
        zoom,
        x: centerX - ((centerX - current.x) / current.zoom) * zoom,
        y: centerY - ((centerY - current.y) / current.zoom) * zoom,
      };
    });
  };

  const focusNode = (node: GraphNode) => {
    const point = positions.get(node.id);
    if (point) {
      setViewport((current) => ({
        ...current,
        x: size.width / 2 - point.x * current.zoom,
        y: size.height / 2 - point.y * current.zoom,
      }));
    }
    dispatch({ type: "SET_TRACE_NODE", payload: node.id });
    setTypeFilter("all");
    setSearch("");
  };

  const types = Object.keys(state.graphStats?.type_distribution ?? {}).sort();
  const activeSelection = graph.nodes.find((node) => node.id === selectedNodeId);

  return (
    <section className="graph-panel" aria-label="Knowledge graph">
      <header className="graph-toolbar">
        <div className="graph-heading">
          <div className="graph-title">Knowledge graph</div>
          <div className="graph-subtitle">
            {graph.nodes.length.toLocaleString()} nodes · {graph.edges.length.toLocaleString()} connections
            {lastUpdated && <span> · Updated {new Date(lastUpdated).toLocaleTimeString()}</span>}
          </div>
        </div>
        <div className="graph-controls">
          <label className="graph-search">
            <span className="sr-only">Search graph nodes</span>
            <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path fillRule="evenodd" d="M8.5 3a5.5 5.5 0 103.447 9.785l3.634 3.634a1 1 0 001.414-1.414l-3.634-3.634A5.5 5.5 0 008.5 3zM5 8.5a3.5 3.5 0 117 0 3.5 3.5 0 01-7 0z" clipRule="evenodd" />
            </svg>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Find a node…" />
          </label>
          <select
            className="graph-filter"
            aria-label="Filter by node type"
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value)}
          >
            <option value="all">All types</option>
            {types.map((type) => <option key={type} value={type}>{type}</option>)}
          </select>
          <button
            className="graph-action"
            onClick={() => setRefreshKey((key) => key + 1)}
            title="Refresh graph"
            aria-label="Refresh graph"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path d="M3 10a7 7 0 0112.04-4.9V3.5a1 1 0 112 0v4h-4a1 1 0 110-2h.58A5 5 0 105 10a1 1 0 11-2 0zm14 0a1 1 0 112 0 7 7 0 01-12.04 4.9v1.6a1 1 0 11-2 0v-4h4a1 1 0 110 2h-.58A5 5 0 0015 10z" />
            </svg>
          </button>
        </div>
        {searchResults.length > 0 && (
          <div className="graph-search-results" role="listbox" aria-label="Matching graph nodes">
            {searchResults.map((node) => (
              <button key={node.id} role="option" aria-selected={node.id === selectedNodeId} onClick={() => focusNode(node)}>
                <span className="graph-result-dot" style={{ backgroundColor: nodeColor(node) }} />
                <span className="graph-result-copy">
                  <strong>{node.summary || node.content.slice(0, 72) || node.node_type}</strong>
                  <small>{node.node_type} · {node.id.slice(0, 14)}</small>
                </span>
              </button>
            ))}
          </div>
        )}
      </header>

      {error && (
        <div className="graph-error" role="alert">
          <span>{error} {graph.nodes.length > 0 ? "Showing the last loaded graph." : ""}</span>
          <button onClick={() => setRefreshKey((key) => key + 1)}>Retry</button>
        </div>
      )}

      <div className="graph-canvas-wrap" ref={containerRef}>
        <canvas
          ref={canvasRef}
          className="graph-canvas"
          aria-label="Interactive project knowledge graph. Drag to pan or move nodes, scroll to zoom, and select a node to inspect its evidence."
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => setHoveredNodeId(null)}
          onPointerCancel={() => { dragRef.current = null; }}
          onWheel={onWheel}
        />
        {loading && graph.nodes.length === 0 && (
          <div className="graph-overlay"><span className="spinner" /> Loading graph…</div>
        )}
        {!loading && graph.nodes.length === 0 && !error && (
          <div className="graph-empty">
            <div className="graph-empty-icon" aria-hidden>
              <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth={1.5}>
                <circle cx="24" cy="24" r="7" />
                <circle cx="9" cy="11" r="4" />
                <circle cx="39" cy="12" r="4" />
                <circle cx="12" cy="38" r="4" />
                <circle cx="38" cy="37" r="4" />
                <path d="m18 19-6-5m18 5 6-5M18 29l-4 6m16-6 5 6" />
              </svg>
            </div>
            <strong>No graph nodes yet</strong>
            <span>Ingest a paper, add a note, or run research to build this project graph.</span>
          </div>
        )}
        <div className="graph-canvas-actions">
          <button onClick={() => zoomBy(1.25)} title="Zoom in" aria-label="Zoom in">+</button>
          <button onClick={() => zoomBy(0.8)} title="Zoom out" aria-label="Zoom out">−</button>
          <button onClick={() => fitGraph(positions)} title="Fit graph to view" aria-label="Fit graph to view">
            <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path d="M3 3h5v2H5v3H3V3zm9 0h5v5h-2V5h-3V3zM3 12h2v3h3v2H3v-5zm12 3v-3h2v5h-5v-2h3z" />
            </svg>
          </button>
        </div>
        <div className="graph-legend" aria-label="Node type legend">
          {types.slice(0, 6).map((type) => (
            <span key={type}><i style={{ backgroundColor: NODE_COLORS[type] ?? NODE_COLORS.generic }} />{type}</span>
          ))}
          {types.length > 6 && <span>+{types.length - 6} types</span>}
        </div>
        <div className="graph-help">Drag to pan · drag a node to move · scroll to zoom</div>
      </div>

      {activeSelection && (
        <div className="graph-selection">
          <span className="graph-selection-dot" style={{ backgroundColor: nodeColor(activeSelection) }} />
          <span className="graph-selection-text">
            <strong>{activeSelection.summary || activeSelection.content.slice(0, 100) || activeSelection.node_type}</strong>
            <small>{activeSelection.node_type} · {activeSelection.status}</small>
          </span>
          <button onClick={() => dispatch({ type: "SET_TRACE_NODE", payload: null })} aria-label="Clear selected graph node">×</button>
        </div>
      )}
    </section>
  );
}
