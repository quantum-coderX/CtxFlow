import React, { useState } from "react";
import { useAppState, useDispatch } from "../store/AppContext";
import { api } from "../api";

interface Toast { id: number; message: string; kind: "good" | "bad" | "info" }

let toastId = 0;
const listeners: Array<(t: Toast) => void> = [];
export const toast = (message: string, kind: Toast["kind"] = "info") => {
  const t = { id: ++toastId, message, kind };
  listeners.forEach((fn) => fn(t));
};

export function ToastContainer() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  React.useEffect(() => {
    const fn = (t: Toast) => {
      setToasts((prev) => [...prev, t]);
      setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== t.id)), 3500);
    };
    listeners.push(fn);
    return () => { const i = listeners.indexOf(fn); if (i > -1) listeners.splice(i, 1); };
  }, []);
  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} className={`toast-item toast-${t.kind}`}>{t.message}</div>
      ))}
    </div>
  );
}

export function Sidebar() {
  const state = useAppState();
  const dispatch = useDispatch();

  return (
    <aside className="sidebar">
      <nav className="sidebar-tabs">
        <button
          className={`sidebar-tab ${state.sidebarTab === "library" ? "active" : ""}`}
          onClick={() => dispatch({ type: "SET_SIDEBAR_TAB", payload: "library" })}
        >
          <svg viewBox="0 0 20 20" fill="currentColor"><path d="M9 4.804A7.968 7.968 0 005.5 4c-1.255 0-2.443.29-3.5.804v10A7.969 7.969 0 015.5 14c1.396 0 2.7.378 3.8 1.035A7.97 7.97 0 0113 14c1.39 0 2.695.373 3.8 1.015v-10A7.968 7.968 0 0013 4c-1.255 0-2.443.29-3.5.804V12a1 1 0 11-2 0V4.804z" /></svg>
          Library
        </button>
        <button
          className={`sidebar-tab ${state.sidebarTab === "history" ? "active" : ""}`}
          onClick={() => dispatch({ type: "SET_SIDEBAR_TAB", payload: "history" })}
        >
          <svg viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clipRule="evenodd" /></svg>
          History
        </button>
      </nav>

      <div className="sidebar-body">
        {state.sidebarTab === "library" ? <LibraryPanel /> : <HistoryPanel />}
      </div>
    </aside>
  );
}

function LibraryPanel() {
  const state = useAppState();
  const dispatch = useDispatch();
  const [loading, setLoading] = useState(false);

  const seedDemo = async () => {
    setLoading(true);
    try {
      const r = await api.demo.seed(state.currentProjectId);
      toast(`Demo: ${r.papers_ingested} papers, ${r.claims_ingested} claims loaded`, "good");
      const h = await api.health();
      dispatch({ type: "SET_HEALTH", payload: h });
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Failed", "bad");
    } finally { setLoading(false); }
  };

  return (
    <div className="library-panel">
      <div className="library-section-head">
        <span className="section-label">Quick actions</span>
      </div>
      <div className="quick-actions">
        <button className="action-card" onClick={seedDemo} disabled={loading}>
          <div className="action-icon bg-blue">
            <svg viewBox="0 0 20 20" fill="currentColor"><path d="M9 4.804A7.968 7.968 0 005.5 4c-1.255 0-2.443.29-3.5.804v10A7.969 7.969 0 015.5 14c1.396 0 2.7.378 3.8 1.035A7.97 7.97 0 0113 14c1.39 0 2.695.373 3.8 1.015v-10A7.968 7.968 0 0013 4c-1.255 0-2.443.29-3.5.804V12a1 1 0 11-2 0V4.804z" /></svg>
          </div>
          <div>
            <div className="action-title">{loading ? "Loading…" : "Load demo corpus"}</div>
            <div className="action-sub">Pre-built research papers</div>
          </div>
        </button>
      </div>

      <div className="library-section-head" style={{ marginTop: 16 }}>
        <span className="section-label">Add paper</span>
      </div>
      <PaperForm projectId={state.currentProjectId} onSuccess={() => {
        api.health().then((h) => dispatch({ type: "SET_HEALTH", payload: h })).catch(() => {});
      }} />

      <div className="library-section-head" style={{ marginTop: 16 }}>
        <span className="section-label">Add note</span>
      </div>
      <NoteForm projectId={state.currentProjectId} />
    </div>
  );
}

function PaperForm({ projectId, onSuccess }: { projectId: string; onSuccess: () => void }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [abstract, setAbstract] = useState("");
  const [authors, setAuthors] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      const r = await api.papers.ingest({
        project_id: projectId, title, abstract,
        authors: authors.split(",").map((a) => a.trim()).filter(Boolean),
      });
      toast(`Ingested: ${r.claim_node_ids.length} claims`, "good");
      setTitle(""); setAbstract(""); setAuthors(""); setOpen(false);
      onSuccess();
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Failed", "bad");
    } finally { setLoading(false); }
  };

  return (
    <div className="collapsible">
      <button className="collapsible-trigger" onClick={() => setOpen(!open)}>
        <svg viewBox="0 0 20 20" fill="currentColor" style={{ width: 14, height: 14, transform: open ? "rotate(90deg)" : "none", transition: "transform 200ms" }}>
          <path fillRule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clipRule="evenodd" />
        </svg>
        Add manually
      </button>
      {open && (
        <form className="ingest-form" onSubmit={submit}>
          <input placeholder="Title *" value={title} onChange={(e) => setTitle(e.target.value)} required />
          <input placeholder="Authors (comma separated)" value={authors} onChange={(e) => setAuthors(e.target.value)} />
          <textarea placeholder="Abstract or key findings *" value={abstract} onChange={(e) => setAbstract(e.target.value)} rows={3} required />
          <button type="submit" className="btn btn-primary btn-sm" disabled={loading}>{loading ? "Ingesting…" : "Ingest paper"}</button>
        </form>
      )}
    </div>
  );
}

function NoteForm({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await api.notes.create({ project_id: projectId, title, content });
      toast("Note saved", "good");
      setTitle(""); setContent(""); setOpen(false);
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Failed", "bad");
    } finally { setLoading(false); }
  };

  return (
    <div className="collapsible">
      <button className="collapsible-trigger" onClick={() => setOpen(!open)}>
        <svg viewBox="0 0 20 20" fill="currentColor" style={{ width: 14, height: 14, transform: open ? "rotate(90deg)" : "none", transition: "transform 200ms" }}>
          <path fillRule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clipRule="evenodd" />
        </svg>
        Add note
      </button>
      {open && (
        <form className="ingest-form" onSubmit={submit}>
          <input placeholder="Note title" value={title} onChange={(e) => setTitle(e.target.value)} />
          <textarea placeholder="Observation, hypothesis… *" value={content} onChange={(e) => setContent(e.target.value)} rows={3} required />
          <button type="submit" className="btn btn-primary btn-sm" disabled={loading}>{loading ? "Saving…" : "Save note"}</button>
        </form>
      )}
    </div>
  );
}

function HistoryPanel() {
  const state = useAppState();
  const dispatch = useDispatch();

  if (!state.runs.length) {
    return (
      <div className="history-empty">
        <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth={1.5} className="history-empty-icon">
          <circle cx="24" cy="24" r="20" />
          <path d="M24 14v10l6 6" strokeLinecap="round" />
        </svg>
        <p>No runs yet. Ask a question to start.</p>
      </div>
    );
  }

  const showRun = (run: typeof state.runs[0]) => {
    // Replay the run in the chat
    dispatch({
      type: "ADD_MESSAGE",
      payload: { id: `user-replay-${run.run_id}`, role: "user", content: run.query, timestamp: run.started_at },
    });
    dispatch({
      type: "ADD_MESSAGE",
      payload: {
        id: `assistant-replay-${run.run_id}`,
        role: "assistant",
        content: run.answer,
        cited_nodes: run.cited_nodes,
        alerts: run.alerts,
        verdict: run.verdict,
        context_usage: run.context_usage,
        timestamp: run.ended_at ?? run.started_at,
      },
    });
  };

  return (
    <ul className="run-list">
      {state.runs.map((run) => (
        <li key={run.run_id} className="run-item" onClick={() => showRun(run)}>
          <div className="run-query">{run.query}</div>
          <div className="run-meta">
            {run.verdict && (
              <span className={`verdict-pill verdict-${run.verdict}`}>
                {run.verdict.replace(/_/g, " ")}
              </span>
            )}
            <span className="run-status">{run.status}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
