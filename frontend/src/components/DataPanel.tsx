import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import { useAppState } from "../store/AppContext";
import type {
  AnalysisResult,
  AnalysisTable,
  ChartRecord,
  DatasetColumn,
  DatasetManifest,
  DatasetVersion,
  OperationInfo,
} from "../types";
import "../data.css";

const NUMERIC = new Set(["integer", "number", "boolean"]);
const CHART_TYPES = ["bar", "line", "scatter", "histogram", "box"] as const;

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "NA";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toPrecision(5);
  if (Array.isArray(value)) return value.map(fmt).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Suggested parameters for an operation; the server validates everything. */
function templateParams(op: string, schema: DatasetColumn[]): Record<string, unknown> {
  const numeric = schema.filter((c) => NUMERIC.has(c.dtype)).map((c) => c.name);
  const categorical = schema.filter((c) => c.dtype === "string").map((c) => c.name);
  const dates = schema.filter((c) => c.dtype === "datetime").map((c) => c.name);
  const [n0 = "", n1 = n0] = numeric;
  const g = categorical[0] ?? "";
  switch (op) {
    case "describe": return {};
    case "missing_summary": return {};
    case "value_counts": return { column: g || n0 };
    case "group_aggregate": return { group_by: [g], aggregations: [{ column: n0, func: "mean" }] };
    case "correlation": return { columns: numeric.slice(0, 4), method: "pearson" };
    case "confidence_interval": return { column: n0, level: 0.95 };
    case "t_test": return { kind: "independent", column: n0, group_column: g, groups: ["", ""] };
    case "mann_whitney": return { column: n0, group_column: g, groups: ["", ""] };
    case "chi_square": return { row_column: categorical[0] ?? "", col_column: categorical[1] ?? "" };
    case "normality_test": return { column: n0 };
    case "linear_regression": return { y: n1, x: numeric.filter((c) => c !== n1).slice(0, 2) };
    case "anova_oneway": return { column: n0, group_column: g };
    case "time_series_summary": return { time_column: dates[0] ?? n0, value_column: n1, window: 3 };
    default: return {};
  }
}

function ResultTable({ table }: { table: AnalysisTable }) {
  return (
    <div className="data-table-wrap">
      <div className="data-caption">{table.name}</div>
      <table className="data-table">
        <thead><tr>{table.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {table.rows.slice(0, 50).map((row, i) => (
            <tr key={i}>{row.map((cell, j) => <td key={j}>{fmt(cell)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DataPanel() {
  const { currentProjectId: projectId } = useAppState();
  const [datasets, setDatasets] = useState<DatasetManifest[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [version, setVersion] = useState<DatasetVersion | null>(null);
  const [operations, setOperations] = useState<OperationInfo[]>([]);
  const [operation, setOperation] = useState("describe");
  const [paramsText, setParamsText] = useState("{}");
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [charts, setCharts] = useState<ChartRecord[]>([]);
  const [chartType, setChartType] = useState<(typeof CHART_TYPES)[number]>("bar");
  const [chartX, setChartX] = useState("");
  const [chartY, setChartY] = useState("");
  const [aggregate, setAggregate] = useState("mean");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const list = await api.datasets.list(projectId);
      setDatasets(list);
      setSelected((current) => (current && list.some((d) => d.dataset_id === current) ? current : list[0]?.dataset_id ?? ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load datasets.");
    }
  }, [projectId]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { api.workstation.operations().then(setOperations).catch(() => setOperations([])); }, []);

  useEffect(() => {
    setResult(null);
    if (!selected) { setVersion(null); setCharts([]); return; }
    api.datasets.get(projectId, selected).then((d) => setVersion(d.version)).catch(() => setVersion(null));
    api.charts.list(projectId, selected).then(setCharts).catch(() => setCharts([]));
  }, [projectId, selected]);

  const schema = useMemo(() => version?.schema ?? [], [version]);
  useEffect(() => {
    setParamsText(JSON.stringify(templateParams(operation, schema), null, 2));
  }, [operation, schema]);
  useEffect(() => {
    setChartX(schema.find((c) => c.dtype === "string")?.name ?? schema[0]?.name ?? "");
    setChartY(schema.find((c) => NUMERIC.has(c.dtype))?.name ?? "");
  }, [schema]);

  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError("");
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
      return undefined;
    } finally {
      setBusy(false);
    }
  };

  const onUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    data.set("project_id", projectId);
    const uploaded = await run(() => api.datasets.upload(data));
    if (uploaded) {
      form.reset();
      await refresh();
      setSelected(uploaded.dataset_id);
      setVersion(uploaded);
    }
  };

  const onAnalyze = async () => {
    let params: Record<string, unknown>;
    try {
      params = JSON.parse(paramsText || "{}");
    } catch {
      setError("Parameters must be valid JSON.");
      return;
    }
    const output = await run(() => api.analyses.run({ project_id: projectId, dataset_id: selected, operation, params }));
    if (output) setResult(output);
  };

  const onChart = async () => {
    const spec: Record<string, unknown> = { chart_type: chartType, dataset_id: selected, title: `${chartY || chartX} by ${chartX}` };
    if (chartType === "histogram") spec.x = chartY || chartX;
    else if (chartType === "box") { spec.y = chartY; spec.x = chartX || undefined; }
    else { spec.x = chartX; spec.y = chartY; }
    if (chartType === "bar") spec.aggregate = aggregate;
    const chart = await run(() => api.charts.create(projectId, spec));
    if (chart) setCharts((previous) => [chart, ...previous.filter((c) => c.chart_id !== chart.chart_id)]);
  };

  return (
    <section className="data-panel" aria-label="Datasets and analysis">
      <div className="data-column">
        <form className="data-card" onSubmit={onUpload}>
          <div className="data-card-title">Upload dataset</div>
          <input name="file" type="file" accept=".csv,.tsv,.json,.jsonl,.xlsx,.parquet" required />
          <input name="name" placeholder="Dataset name (same name adds a version)" />
          <button className="btn btn-primary btn-sm" disabled={busy}>Upload</button>
        </form>

        <div className="data-card">
          <div className="data-card-title">Datasets</div>
          {!datasets.length && <p className="data-muted">No datasets in this project yet.</p>}
          <ul className="data-list">
            {datasets.map((d) => (
              <li key={d.dataset_id}>
                <button className={`data-item ${d.dataset_id === selected ? "active" : ""}`} onClick={() => setSelected(d.dataset_id)}>
                  <span>{d.name}</span>
                  <span className="data-muted">v{d.latest_version} · {d.versions.at(-1)?.row_count ?? 0} rows</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        {version && (
          <div className="data-card">
            <div className="data-card-title">Profile · {version.name} v{version.version}</div>
            <div className="data-muted mono">sha256 {version.content_hash.slice(0, 16)}…</div>
            <table className="data-table">
              <thead><tr><th>column</th><th>type</th><th>missing</th><th>mean / top</th></tr></thead>
              <tbody>
                {version.profile.columns.map((c) => (
                  <tr key={c.name}>
                    <td>{c.name}</td>
                    <td>{c.dtype}</td>
                    <td>{c.missing} ({fmt(c.missing_pct)}%)</td>
                    <td>{"mean" in c ? fmt(c.mean) : fmt((c.top_values as { value: unknown }[] | undefined)?.[0]?.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {version.warnings.map((w) => <div key={w} className="data-warning">{w}</div>)}
          </div>
        )}
      </div>

      <div className="data-column wide">
        {error && <div className="data-error" role="alert">{error}</div>}
        {selected ? (
          <>
            <div className="data-card">
              <div className="data-card-title">Analysis (fixed operation catalog)</div>
              <select value={operation} onChange={(e) => setOperation(e.target.value)}>
                {operations.map((op) => <option key={op.name} value={op.name}>{op.name}</option>)}
              </select>
              <div className="data-muted">{operations.find((op) => op.name === operation)?.description}</div>
              <textarea className="mono" rows={6} value={paramsText} onChange={(e) => setParamsText(e.target.value)} aria-label="Parameters (JSON)" />
              <button className="btn btn-primary btn-sm" onClick={onAnalyze} disabled={busy}>Run analysis</button>
            </div>

            {result && (
              <div className="data-card">
                <div className="data-card-title">{result.operation} · {result.dataset.dataset_id} v{result.dataset.version}</div>
                <div className="data-muted mono">{result.analysis_id}</div>
                <dl className="data-values">
                  {Object.entries(result.values).map(([k, v]) => (
                    <div key={k}><dt>{k}</dt><dd>{fmt(v)}</dd></div>
                  ))}
                </dl>
                {result.tables.map((t) => <ResultTable key={t.name} table={t} />)}
                {result.warnings.map((w) => <div key={w} className="data-warning">{w}</div>)}
              </div>
            )}

            <div className="data-card">
              <div className="data-card-title">Chart</div>
              <div className="data-row">
                <select value={chartType} onChange={(e) => setChartType(e.target.value as typeof chartType)}>
                  {CHART_TYPES.map((t) => <option key={t}>{t}</option>)}
                </select>
                <select value={chartX} onChange={(e) => setChartX(e.target.value)} aria-label="x">
                  {schema.map((c) => <option key={c.name} value={c.name}>x: {c.name}</option>)}
                </select>
                <select value={chartY} onChange={(e) => setChartY(e.target.value)} aria-label="y">
                  <option value="">y: (none)</option>
                  {schema.filter((c) => NUMERIC.has(c.dtype)).map((c) => <option key={c.name} value={c.name}>y: {c.name}</option>)}
                </select>
                {chartType === "bar" && (
                  <select value={aggregate} onChange={(e) => setAggregate(e.target.value)} aria-label="aggregate">
                    {["mean", "sum", "median", "count"].map((a) => <option key={a}>{a}</option>)}
                  </select>
                )}
                <button className="btn btn-primary btn-sm" onClick={onChart} disabled={busy}>Render</button>
              </div>
              <div className="data-charts">
                {charts.map((chart) => (
                  <figure key={chart.chart_id}>
                    <img src={api.charts.imageUrl(projectId, chart.chart_id)} alt={String(chart.spec.title ?? chart.chart_id)} />
                    <figcaption className="data-muted mono">{chart.chart_id} · {chart.dataset.dataset_id} v{chart.dataset.version}</figcaption>
                  </figure>
                ))}
              </div>
            </div>
          </>
        ) : (
          <p className="data-muted">Upload a CSV, TSV, JSON, XLSX or Parquet file to begin.</p>
        )}
      </div>
    </section>
  );
}
