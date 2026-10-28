import { useState, useEffect } from "react";
import { useAppState, useDispatch } from "../store/AppContext";
import { api } from "../api";
import type { TraceResponse } from "../types";

type TraceTab = "supporting" | "superseded" | "contradictions" | "sources";

export function TracePanel() {
  const state = useAppState();
  const dispatch = useDispatch();
  const nodeId = state.traceNodeId;

  const [trace, setTrace] = useState<TraceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<TraceTab>("supporting");

  useEffect(() => {
    if (!nodeId) { setTrace(null); return; }
    setLoading(true);
    setTrace(null);
    api.trace(nodeId, state.currentProjectId)
      .then(setTrace)
      .catch(() => setTrace(null))
      .finally(() => setLoading(false));
  }, [nodeId, state.currentProjectId]);

  if (!nodeId) {
    return (
      <aside className="trace-panel trace-empty-panel">
        <div className="trace-empty">
          <div className="trace-empty-icon" aria-hidden>
            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth={1.5}>
              <circle cx="24" cy="24" r="8" />
              <circle cx="8"  cy="40" r="5" />
              <circle cx="40" cy="40" r="5" />
              <circle cx="24" cy="6"  r="5" />
              <line x1="24" y1="16" x2="24" y2="19" />
              <line x1="16" y1="27" x2="12" y2="36" />
              <line x1="32" y1="27" x2="36" y2="36" />
            </svg>
          </div>
          <h3>Evidence &amp; Trace</h3>
          <p>Click a <code>[Node:…]</code> citation in an answer to inspect its provenance chain, superseded claims, and contradictions.</p>
        </div>
      </aside>
    );
  }

  const tabs: { id: TraceTab; label: string; count: number }[] = [
    { id: "supporting",     label: "Supporting",     count: trace?.provenance_chain.length ?? 0 },
    { id: "superseded",     label: "Superseded",     count: trace?.superseded_claims.length ?? 0 },
    { id: "contradictions", label: "Contradictions", count: trace?.contradictions.length ?? 0 },
    { id: "sources",        label: "Sources",        count: trace?.source_papers.length ?? 0 },
  ];

  return (
    <aside className="trace-panel">
      <div className="trace-header">
        <div>
          <div className="trace-header-label">Evidence Trace</div>
          <code className="trace-node-id">{nodeId}</code>
        </div>
        <button
          className="btn-icon"
          onClick={() => dispatch({ type: "SET_TRACE_NODE", payload: null })}
          title="Close"
          aria-label="Close trace panel"
        >
          <svg viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
          </svg>
        </button>
      </div>

      {/* Tab bar */}
      <div className="trace-tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            className={`trace-tab ${tab === t.id ? "active" : ""}`}
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.count > 0 && <span className="tab-count">{t.count}</span>}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="trace-content" role="tabpanel">
        {loading && (
          <div className="trace-loading">
            <span className="spinner" aria-label="Loading trace" />
          </div>
        )}

        {!loading && tab === "supporting" && (
          <ul className="trace-list">
            {(trace?.provenance_chain ?? []).length === 0
              ? <li className="trace-empty-item">No supporting chain found</li>
              : trace!.provenance_chain.map((step, i) => (
                <li key={i} className="trace-item">
                  {step.edge_relation && <div className="trace-relation">{step.edge_relation}</div>}
                  <div className="trace-body">{step.content.slice(0, 280)}</div>
                  <div className="trace-type">{step.node_type} · {step.status}</div>
                </li>
              ))}
          </ul>
        )}

        {!loading && tab === "superseded" && (
          <ul className="trace-list">
            {(trace?.superseded_claims ?? []).length === 0
              ? <li className="trace-empty-item">No superseded claims</li>
              : trace!.superseded_claims.map((n) => (
                <li key={n.id} className="trace-item trace-superseded">
                  <div className="trace-body">{n.content.slice(0, 280)}</div>
                  <div className="trace-type">{n.node_type} · superseded</div>
                </li>
              ))}
          </ul>
        )}

        {!loading && tab === "contradictions" && (
          <ul className="trace-list">
            {(trace?.contradictions ?? []).length === 0
              ? <li className="trace-empty-item">No contradictions detected ✓</li>
              : trace!.contradictions.map((c, i) => (
                <li key={i} className="trace-item trace-contradiction">
                  <div className="trace-relation">contradicts via {c.relation}</div>
                  <div className="trace-body">{c.content.slice(0, 280)}</div>
                </li>
              ))}
          </ul>
        )}

        {!loading && tab === "sources" && (
          <ul className="trace-list">
            {(trace?.source_papers ?? []).length === 0
              ? <li className="trace-empty-item">No source papers linked</li>
              : trace!.source_papers.map((n) => (
                <li key={n.id} className="trace-item">
                  <div className="trace-body font-medium">{(n.metadata?.title as string) || n.content.slice(0, 120)}</div>
                  {n.metadata?.year != null && <div className="trace-type">{`${n.metadata.year as string | number}`}</div>}
                </li>
              ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
