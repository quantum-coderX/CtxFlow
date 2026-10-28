// API client — thin wrappers over fetch

import type {
  HealthResponse,
  GraphData,
  GraphStats,
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
  },
};
