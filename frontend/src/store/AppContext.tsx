import { createContext, useContext, useReducer } from "react";
import type { ReactNode } from "react";
import type {
  ChatMessage,
  ProjectMeta,
  RunRecord,
  HealthResponse,
  GraphStats,
  StageState,
  TaskProgress,
  TaskStatus,
  WorkflowEvent,
} from "../types";

// ─── State ───────────────────────────────────────────────────────────────────

interface AppState {
  health: HealthResponse | null;
  graphStats: GraphStats | null;
  projects: ProjectMeta[];
  currentProjectId: string;
  messages: ChatMessage[];
  runs: RunRecord[];
  busy: boolean;
  stageState: StageState;
  tasks: TaskProgress[];
  activeRunId: string | null;
  elapsedMs: number;
  traceNodeId: string | null;
  sidebarTab: "library" | "history";
}

const initialState: AppState = {
  health: null,
  graphStats: null,
  projects: [],
  currentProjectId: "default",
  messages: [],
  runs: [],
  busy: false,
  stageState: {},
  tasks: [],
  activeRunId: null,
  elapsedMs: 0,
  traceNodeId: null,
  sidebarTab: "library",
};

// ─── Actions ─────────────────────────────────────────────────────────────────

type Action =
  | { type: "SET_HEALTH"; payload: HealthResponse }
  | { type: "SET_GRAPH_STATS"; payload: GraphStats }
  | { type: "SET_PROJECTS"; payload: ProjectMeta[] }
  | { type: "ADD_PROJECT"; payload: ProjectMeta }
  | { type: "SET_PROJECT"; payload: string }
  | { type: "RESTORE_CONVERSATION"; payload: ChatMessage[] }
  | { type: "ADD_MESSAGE"; payload: ChatMessage }
  | { type: "UPDATE_MESSAGE"; id: string; patch: Partial<ChatMessage> }
  | { type: "SET_BUSY"; payload: boolean }
  | { type: "SET_STAGE"; stage: string; status: string }
  | { type: "RESET_STAGES" }
  | { type: "SET_TASKS"; payload: TaskProgress[] }
  | { type: "TASK_EVENT"; payload: WorkflowEvent }
  | { type: "CLOSE_TASKS" }
  | { type: "SET_ACTIVE_RUN"; payload: string | null }
  | { type: "SET_ELAPSED"; payload: number }
  | { type: "SET_RUNS"; payload: RunRecord[] }
  | { type: "SET_TRACE_NODE"; payload: string | null }
  | { type: "SET_SIDEBAR_TAB"; payload: "library" | "history" };

function applyTaskEvent(tasks: TaskProgress[], ev: WorkflowEvent): TaskProgress[] {
  if (!ev.task_id) return tasks;
  const data = (ev.data ?? {}) as Record<string, unknown>;
  return tasks.map((task) => {
    if (task.task_id !== ev.task_id) return task;
    if (ev.kind === "task_started") return { ...task, status: "running" as TaskStatus };
    if (ev.kind === "task_skipped") {
      const reason = typeof data.reason === "string" ? data.reason : ev.message;
      if (!ev.provider) return { ...task, status: "skipped" as TaskStatus, reason };
      return task;
    }
    if (!ev.provider) return task;
    const progress = {
      provider: ev.provider,
      status: ev.status,
      result_count: typeof data.result_count === "number" ? data.result_count : undefined,
      elapsed_ms: typeof data.elapsed_ms === "number" ? data.elapsed_ms : undefined,
      error: ev.kind === "task_failed" ? ev.message : undefined,
    };
    return { ...task, providers: [...task.providers, progress] };
  });
}

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_HEALTH":    return { ...state, health: action.payload };
    case "SET_GRAPH_STATS": return { ...state, graphStats: action.payload };
    case "SET_PROJECTS":  return { ...state, projects: action.payload };
    case "ADD_PROJECT":   return { ...state, projects: [...state.projects, action.payload] };
    case "SET_PROJECT":  return { ...state, currentProjectId: action.payload, graphStats: null, traceNodeId: null, messages: [], runs: [], stageState: {}, tasks: [] };
    case "RESTORE_CONVERSATION": {
      const byId = new Map<string, ChatMessage>();
      for (const message of [...action.payload, ...state.messages]) {
        byId.set(message.id, message);
      }
      return {
        ...state,
        messages: [...byId.values()].sort((left, right) => left.timestamp - right.timestamp),
      };
    }
    case "ADD_MESSAGE":
      return state.messages.some((message) => message.id === action.payload.id)
        ? state
        : { ...state, messages: [...state.messages, action.payload] };
    case "UPDATE_MESSAGE":
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.id === action.id ? { ...m, ...action.patch } : m
        ),
      };
    case "SET_BUSY":      return { ...state, busy: action.payload };
    case "SET_STAGE":
      return { ...state, stageState: { ...state.stageState, [action.stage]: action.status as StageState[string] } };
    case "RESET_STAGES":  return { ...state, stageState: {}, tasks: [] };
    case "SET_TASKS":     return { ...state, tasks: action.payload };
    case "TASK_EVENT":    return { ...state, tasks: applyTaskEvent(state.tasks, action.payload) };
    case "CLOSE_TASKS":
      // A dropped connection must not leave tasks spinning forever.
      return {
        ...state,
        tasks: state.tasks.map((t) =>
          t.status === "running" || t.status === "pending" ? { ...t, status: "failed" as TaskStatus } : t
        ),
      };
    case "SET_ACTIVE_RUN": return { ...state, activeRunId: action.payload };
    case "SET_ELAPSED":   return { ...state, elapsedMs: action.payload };
    case "SET_RUNS":      return { ...state, runs: action.payload };
    case "SET_TRACE_NODE": return { ...state, traceNodeId: action.payload };
    case "SET_SIDEBAR_TAB": return { ...state, sidebarTab: action.payload };
    default:              return state;
  }
}

// ─── Context ─────────────────────────────────────────────────────────────────

interface AppContextValue {
  state: AppState;
  dispatch: React.Dispatch<Action>;
}

const AppContext = createContext<AppContextValue | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return <AppContext.Provider value={{ state, dispatch }}>{children}</AppContext.Provider>;
}

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used within AppProvider");
  return ctx;
}

// ─── Convenience hooks ────────────────────────────────────────────────────────

export function useDispatch() {
  return useApp().dispatch;
}

export function useAppState() {
  return useApp().state;
}
