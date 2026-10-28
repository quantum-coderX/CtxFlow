import React from "react";
import { useAppState, useDispatch } from "../store/AppContext";
import { api } from "../api";

export function TopBar() {
  const state = useAppState();
  const dispatch = useDispatch();
  const h = state.health;

  const online = h?.status === "ok";

  return (
    <header className="topbar">
      {/* Brand */}
      <div className="brand">
        <div className="brand-icon" aria-hidden>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <path d="M12 2L2 7l10 5 10-5-10-5z" />
            <path d="M2 17l10 5 10-5" />
            <path d="M2 12l10 5 10-5" />
          </svg>
        </div>
        <div>
          <span className="brand-name">ResearchOS</span>
          <span className="brand-sub">Multi-Agent Research</span>
        </div>
      </div>

      {/* Project selector */}
      <div className="project-selector">
        <label className="project-label" htmlFor="project-select">Project</label>
        <select
          id="project-select"
          value={state.currentProjectId}
          onChange={(e) => dispatch({ type: "SET_PROJECT", payload: e.target.value })}
        >
          {state.projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>
              {p.name || p.project_id}
            </option>
          ))}
          {state.projects.length === 0 && (
            <option value="default">default</option>
          )}
        </select>
        <NewProjectButton />
      </div>

      {/* Right: status + model badge + stats */}
      <div className="topbar-right">
        {h?.model_enabled && (
          <div className="model-badge" title={`${h.model_provider} · ${h.model_id}`}>
            <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path fillRule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clipRule="evenodd" />
            </svg>
            {(h.model_provider ?? "AI").toUpperCase()}
          </div>
        )}

        <div className="graph-stats">
          <span><strong>{state.graphStats?.total_nodes ?? h?.graph_nodes ?? 0}</strong> nodes</span>
          <span><strong>{state.graphStats?.total_edges ?? h?.graph_edges ?? 0}</strong> edges</span>
        </div>

        <div className="status-pill">
          <span className={`status-dot ${online ? "ok" : "bad"}`} />
          <span className="status-text">{online ? `v${h!.version}` : "offline"}</span>
        </div>
      </div>
    </header>
  );
}

function NewProjectButton() {
  const dispatch = useDispatch();
  const [open, setOpen] = React.useState(false);
  const [id, setId] = React.useState("");
  const [name, setName] = React.useState("");
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const p = await api.projects.create({ project_id: id.trim(), name: name.trim() });
      dispatch({ type: "ADD_PROJECT", payload: p });
      dispatch({ type: "SET_PROJECT", payload: p.project_id });
      setOpen(false);
      setId(""); setName("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <button className="btn-icon" onClick={() => setOpen(true)} title="New project" aria-label="Create new project">
        <svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 3a1 1 0 011 1v5h5a1 1 0 110 2h-5v5a1 1 0 11-2 0v-5H4a1 1 0 110-2h5V4a1 1 0 011-1z" /></svg>
      </button>
      {open && (
        <div className="dialog-overlay" role="dialog" aria-modal aria-label="New project">
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <h2>New Project</h2>
            <form className="dialog-form" onSubmit={submit}>
              <label className="field-label">Project ID <span className="required">*</span></label>
              <input value={id} onChange={(e) => setId(e.target.value)} placeholder="e.g. graphrag-study" required pattern="[A-Za-z0-9._-]{1,64}" />
              <label className="field-label">Display name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="GraphRAG Research" />
              {error && <p className="error-text">{error}</p>}
              <div className="dialog-actions">
                <button type="button" className="btn btn-ghost" onClick={() => { setOpen(false); setError(""); }}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={loading}>{loading ? "Creating…" : "Create"}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
