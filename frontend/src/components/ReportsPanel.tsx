import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useAppState } from "../store/AppContext";
import type { AnalysisResult, ArtifactRecord, ArtifactSummary, ChartRecord, Staleness } from "../types";
import "../data.css";

const TEMPLATES = ["literature_review", "technical_report", "paper_summary", "manuscript"];
const DOWNLOADS = ["markdown", "html", "latex", "bibtex", "pdf"];

export function ReportsPanel() {
  const { currentProjectId: projectId } = useAppState();
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisResult[]>([]);
  const [charts, setCharts] = useState<ChartRecord[]>([]);
  const [topic, setTopic] = useState("");
  const [template, setTemplate] = useState("literature_review");
  const [pickedAnalyses, setPickedAnalyses] = useState<string[]>([]);
  const [pickedCharts, setPickedCharts] = useState<string[]>([]);
  const [selected, setSelected] = useState<ArtifactRecord | null>(null);
  const [staleness, setStaleness] = useState<Staleness | null>(null);
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [list, a, c] = await Promise.all([
        api.artifacts.list(projectId), api.analyses.list(projectId), api.charts.list(projectId),
      ]);
      setArtifacts(list);
      setAnalyses(a);
      setCharts(c);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load reports.");
    }
  }, [projectId]);

  useEffect(() => { void refresh(); setSelected(null); }, [refresh]);

  const open = useCallback(async (artifactId: string) => {
    setError("");
    try {
      const [record, stale] = await Promise.all([
        api.artifacts.get(projectId, artifactId), api.artifacts.staleness(projectId, artifactId),
      ]);
      setSelected(record);
      setStaleness(stale);
      const markdownOk = record.builds.at(-1)?.outputs.markdown?.status === "ok";
      setPreview(markdownOk ? await api.artifacts.text(projectId, artifactId, "markdown") : "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open report.");
    }
  }, [projectId]);

  const act = async (fn: () => Promise<ArtifactRecord>) => {
    setBusy(true);
    setError("");
    try {
      const record = await fn();
      await refresh();
      await open(record.artifact_id);
      if (record.compose_warnings?.length) setError(record.compose_warnings.join(" "));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  };

  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  const build = selected?.builds.at(-1);

  return (
    <section className="data-panel" aria-label="Reports">
      <div className="data-column">
        <div className="data-card">
          <div className="data-card-title">Compose report from workspace evidence</div>
          <input placeholder="Topic (optional)" value={topic} onChange={(e) => setTopic(e.target.value)} />
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>
            {TEMPLATES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
          </select>
          {!!analyses.length && <div className="data-muted">Include analyses</div>}
          {analyses.slice(0, 12).map((a) => (
            <label key={a.analysis_id} className="data-check">
              <input type="checkbox" checked={pickedAnalyses.includes(a.analysis_id)}
                onChange={() => setPickedAnalyses((p) => toggle(p, a.analysis_id))} />
              {a.operation} · {a.dataset.dataset_id} v{a.dataset.version}
            </label>
          ))}
          {!!charts.length && <div className="data-muted">Include charts</div>}
          {charts.slice(0, 12).map((c) => (
            <label key={c.chart_id} className="data-check">
              <input type="checkbox" checked={pickedCharts.includes(c.chart_id)}
                onChange={() => setPickedCharts((p) => toggle(p, c.chart_id))} />
              {String(c.spec.chart_type)} · {c.dataset.dataset_id} v{c.dataset.version}
            </label>
          ))}
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => act(() => api.artifacts.compose({
            project_id: projectId, topic, template, analysis_ids: pickedAnalyses, chart_ids: pickedCharts,
          }))}>{busy ? "Building…" : "Compose & build"}</button>
        </div>

        <div className="data-card">
          <div className="data-card-title">Artifacts</div>
          {!artifacts.length && <p className="data-muted">No reports yet.</p>}
          <ul className="data-list">
            {artifacts.map((a) => (
              <li key={a.artifact_id}>
                <button className={`data-item ${selected?.artifact_id === a.artifact_id ? "active" : ""}`} onClick={() => open(a.artifact_id)}>
                  <span>{a.title}</span>
                  <span className={`status-pill status-${a.status}`}>{a.status} · v{a.latest_version}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="data-column wide">
        {error && <div className="data-error" role="alert">{error}</div>}
        {selected && build ? (
          <div className="data-card">
            <div className="data-card-title">{selected.title} · v{build.version}</div>
            <div className="data-row">
              <span className={`status-pill status-${build.status}`}>{build.status}</span>
              {Object.entries(build.outputs).map(([fmt, out]) => (
                <span key={fmt} className={`status-pill status-${out.status}`} title={out.error ?? ""}>{fmt}: {out.status}</span>
              ))}
            </div>
            {staleness?.stale && (
              <div className="data-warning">
                Stale: {staleness.reasons.join(" ")}
                <button className="btn btn-sm" disabled={busy}
                  onClick={() => act(() => api.artifacts.build(projectId, selected.artifact_id, true))}>Rebuild with current inputs</button>
              </div>
            )}
            {build.errors.map((e) => <div key={e} className="data-error">{e}</div>)}
            {build.warnings.map((w) => <div key={w} className="data-warning">{w}</div>)}
            {build.outputs.pdf?.error && <div className="data-warning">PDF: {build.outputs.pdf.error}</div>}
            <div className="data-row">
              {DOWNLOADS.filter((f) => f === "bibtex" ? build.outputs.latex?.status === "ok" : build.outputs[f]?.status === "ok").map((f) => (
                <a key={f} className="btn btn-sm" href={api.artifacts.fileUrl(projectId, selected.artifact_id, f, true)}>{f}</a>
              ))}
              <button className="btn btn-sm" disabled={busy}
                onClick={() => act(() => api.artifacts.build(projectId, selected.artifact_id))}>Rebuild</button>
            </div>
            {preview && <pre className="data-preview">{preview}</pre>}
          </div>
        ) : (
          <p className="data-muted">Compose a report or open an artifact to see its build, citations and staleness.</p>
        )}
      </div>
    </section>
  );
}
