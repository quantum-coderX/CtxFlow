// API client — thin wrappers over fetch

import type {
  AnalysisResult,
  ArtifactRecord,
  ArtifactSummary,
  ChartRecord,
  DatasetManifest,
  DatasetVersion,
  OperationInfo,
  Staleness,
  WorkstationCapabilities,
  HealthResponse,
  GraphData,
  GraphPage,
  GraphStats,
  MemoryPage,
  ProjectMeta,
  RunRecord,
  TraceResponse,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {}
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: string }).detail
        : `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return body as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  health: () => request<HealthResponse>("/health"),

  projects: {
    list: () =>
      request<{ projects: ProjectMeta[] }>("/api/projects").then(
        (d) => d.projects
      ),
    create: (data: { project_id: string; name?: string; description?: string }) =>
      post<ProjectMeta>("/api/projects", data),
  },

  runs: {
    list: (project_id?: string, session_id?: string, limit = 100) => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (project_id) qs.set("project_id", project_id);
      if (session_id) qs.set("session_id", session_id);
      return request<{ runs: RunRecord[] }>(`/api/runs?${qs}`).then(
        (d) => d.runs
      );
    },
    get: (run_id: string) => request<RunRecord>(`/api/runs/${run_id}`),
  },

  papers: {
    ingest: (data: {
      project_id: string;
      title: string;
      abstract: string;
      authors?: string[];
      year?: number | null;
      doi?: string;
    }) => post<{ paper_node_id: string; claim_node_ids: string[]; message: string }>("/api/papers/ingest", data),
    uploadForm: (form: FormData) =>
      request<{ paper_node_id: string; claim_node_ids: string[]; message: string }>("/api/papers/upload", {
        method: "POST",
        body: form,
      }),
  },

  notes: {
    list: (project_id: string) =>
      request<{ notes: { node_id: string; title: string; content: string }[] }>(
        `/api/notes?project_id=${encodeURIComponent(project_id)}`
      ).then((d) => d.notes),
    create: (data: { project_id: string; title?: string; content: string }) =>
      post<{ node_id: string }>("/api/notes", data),
  },

  demo: {
    seed: (project_id: string) =>
      post<{ papers_ingested: number; claims_ingested: number; message: string }>(
        "/api/demo/seed",
        { project_id }
      ),
  },

  trace: (node_id: string, project_id: string) =>
    post<TraceResponse>(`/api/trace?project_id=${encodeURIComponent(project_id)}`, { node_id }),

  graph: {
    stats: (project_id: string) =>
      request<GraphStats>(
        `/api/graph/stats?project_id=${encodeURIComponent(project_id)}`,
        { cache: "no-store" }
      ),
    data: (project_id: string) =>
      request<GraphData>(
        `/api/graph/data?project_id=${encodeURIComponent(project_id)}`,
        { cache: "no-store" }
      ),
    page: (project_id: string, cursor: string | null, limit = GRAPH_PAGE_SIZE) =>
      request<GraphPage>(
        `/api/graph/page?project_id=${encodeURIComponent(project_id)}&limit=${limit}` +
          (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""),
        { cache: "no-store" }
      ),
  },
  memory: {
    page: (project_id: string, cursor: string | null, tier: string | null, limit = 50) =>
      request<MemoryPage>(
        `/api/memory/page?project_id=${encodeURIComponent(project_id)}&limit=${limit}` +
          (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "") +
          (tier ? `&tier=${tier}` : ""),
        { cache: "no-store" }
      ),
  },

  workstation: {
    capabilities: () => request<WorkstationCapabilities>("/api/workstation/capabilities"),
    operations: () => request<{ operations: OperationInfo[] }>("/api/analytics/operations").then((d) => d.operations),
  },

  datasets: {
    list: (project_id: string) =>
      request<{ datasets: DatasetManifest[] }>(`/api/datasets?project_id=${q(project_id)}`, { cache: "no-store" })
        .then((d) => d.datasets),
    get: (project_id: string, dataset_id: string, version?: number) =>
      request<{ dataset: DatasetManifest; version: DatasetVersion }>(
        `/api/datasets/${q(dataset_id)}?project_id=${q(project_id)}` + (version ? `&version=${version}` : ""),
        { cache: "no-store" }
      ),
    upload: (form: FormData) => request<DatasetVersion>("/api/datasets/upload", { method: "POST", body: form }),
  },

  analyses: {
    run: (data: { project_id: string; dataset_id: string; operation: string; params: Record<string, unknown>; version?: number }) =>
      post<AnalysisResult>("/api/analyses", data),
    list: (project_id: string, dataset_id?: string) =>
      request<{ analyses: AnalysisResult[] }>(
        `/api/analyses?project_id=${q(project_id)}` + (dataset_id ? `&dataset_id=${q(dataset_id)}` : ""),
        { cache: "no-store" }
      ).then((d) => d.analyses),
  },

  charts: {
    create: (project_id: string, spec: Record<string, unknown>) =>
      post<ChartRecord>("/api/charts", { project_id, spec }),
    list: (project_id: string, dataset_id?: string) =>
      request<{ charts: ChartRecord[] }>(
        `/api/charts?project_id=${q(project_id)}` + (dataset_id ? `&dataset_id=${q(dataset_id)}` : ""),
        { cache: "no-store" }
      ).then((d) => d.charts),
    imageUrl: (project_id: string, chart_id: string, format: "png" | "svg" = "png") =>
      `/api/charts/${q(chart_id)}/image?project_id=${q(project_id)}&format=${format}`,
  },

  artifacts: {
    list: (project_id: string) =>
      request<{ artifacts: ArtifactSummary[] }>(`/api/artifacts?project_id=${q(project_id)}`, { cache: "no-store" })
        .then((d) => d.artifacts),
    get: (project_id: string, artifact_id: string) =>
      request<ArtifactRecord>(`/api/artifacts/${q(artifact_id)}?project_id=${q(project_id)}`, { cache: "no-store" }),
    compose: (data: {
      project_id: string; topic?: string; title?: string; template?: string;
      analysis_ids?: string[]; chart_ids?: string[]; use_model?: boolean;
    }) => post<ArtifactRecord>("/api/artifacts/compose", data),
    build: (project_id: string, artifact_id: string, refresh_inputs = false) =>
      post<ArtifactRecord>(`/api/artifacts/${q(artifact_id)}/build`, { project_id, refresh_inputs }),
    staleness: (project_id: string, artifact_id: string) =>
      request<Staleness>(`/api/artifacts/${q(artifact_id)}/staleness?project_id=${q(project_id)}`, { cache: "no-store" }),
    fileUrl: (project_id: string, artifact_id: string, fmt: string, download = false) =>
      `/api/artifacts/${q(artifact_id)}/files/${fmt}?project_id=${q(project_id)}` + (download ? "&download=true" : ""),
    text: async (project_id: string, artifact_id: string, fmt: string) => {
      const res = await fetch(api.artifacts.fileUrl(project_id, artifact_id, fmt), { cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.text();
    },
  },
};

function q(value: string): string {
  return encodeURIComponent(value);
}

export const GRAPH_PAGE_SIZE = 500;
