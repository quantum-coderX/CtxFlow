import { useCallback, useRef } from "react";
import { useApp } from "../store/AppContext";
import type { ChatMessage, ResearchPlan, TaskProgress, WorkflowEvent } from "../types";
import { WORKFLOW_STAGES } from "../types";
import { api } from "../api";
import { getConversationSessionId } from "../session";

let msgCounter = 0;
const uid = () => `msg-${Date.now()}-${++msgCounter}`;

export function useWorkflow() {
  const { state, dispatch } = useApp();
  const sourceRef = useRef<EventSource | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startRef = useRef<number>(0);

  const stopTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const startTimer = useCallback(() => {
    stopTimer();
    startRef.current = Date.now();
    timerRef.current = setInterval(() => {
      dispatch({ type: "SET_ELAPSED", payload: Date.now() - startRef.current });
    }, 200);
  }, [dispatch, stopTimer]);

  const run = useCallback(
    (query: string) => {
      if (state.busy) return;

      // User message
      const userMsg: ChatMessage = {
        id: uid(),
        role: "user",
        content: query,
        timestamp: Date.now(),
      };
      dispatch({ type: "ADD_MESSAGE", payload: userMsg });
      dispatch({ type: "SET_BUSY", payload: true });
      dispatch({ type: "RESET_STAGES" });
      startTimer();

      // Thinking placeholder
      const thinkId = uid();
      const thinkMsg: ChatMessage = {
        id: thinkId,
        role: "thinking",
        content: "",
        timestamp: Date.now(),
      };
      dispatch({ type: "ADD_MESSAGE", payload: thinkMsg });

      // Build SSE URL
      const params = new URLSearchParams({
        query,
        project_id: state.currentProjectId,
        session_id: getConversationSessionId(state.currentProjectId),
        token_budget: "2048",
      });

      if (sourceRef.current) {
        try { sourceRef.current.close(); } catch {}
      }

      const source = new EventSource(`/api/stream?${params}`);
      sourceRef.current = source;

      const safeJson = (s: string): Record<string, unknown> => {
        try { return JSON.parse(s) as Record<string, unknown>; } catch { return {}; }
      };

      // Start event
      source.addEventListener("start", (e: MessageEvent) => {
        const d = safeJson(e.data);
        if (d.run_id) dispatch({ type: "SET_ACTIVE_RUN", payload: d.run_id as string });
      });

      // Per-stage events
      for (const stage of WORKFLOW_STAGES) {
        source.addEventListener(stage, (e: MessageEvent) => {
          const d = safeJson(e.data);
          dispatch({ type: "SET_STAGE", stage, status: (d.status as string) || "active" });
        });
      }

      // The validated plan arrives with the completed plan stage.
      source.addEventListener("plan", (e: MessageEvent) => {
        const d = safeJson(e.data);
        const plan = (d.data as { research_plan?: ResearchPlan } | undefined)?.research_plan;
        if (plan?.subtasks?.length) {
          dispatch({
            type: "SET_TASKS",
            payload: plan.subtasks.map((t) => ({
              task_id: t.task_id,
              query: t.query,
              purpose: t.purpose,
              status: "pending" as const,
              providers: [],
            })),
          });
        }
      });

      // Task-level progress inside the retrieve stage.
      source.addEventListener("task", (e: MessageEvent) => {
        dispatch({ type: "TASK_EVENT", payload: safeJson(e.data) as unknown as WorkflowEvent });
      });

      const finish = (data: Record<string, unknown> | null) => {
        source.close();
        sourceRef.current = null;
        stopTimer();

        if (data) {
          const finalTasks = data.tasks as TaskProgress[] | undefined;
          if (Array.isArray(finalTasks) && finalTasks.length) {
            dispatch({
              type: "SET_TASKS",
              payload: finalTasks.map((t) => ({
                ...t,
                providers: (t as unknown as { provider_results?: TaskProgress["providers"] }).provider_results ?? t.providers ?? [],
              })),
            });
          } else {
            dispatch({ type: "CLOSE_TASKS" });
          }
        } else {
          dispatch({ type: "CLOSE_TASKS" });
        }

        // Mark all pending stages done
        for (const s of WORKFLOW_STAGES) {
          dispatch({ type: "SET_STAGE", stage: s, status: data ? "done" : "failed" });
        }

        const answer = (data?.answer as string) || "(No answer — graph may be empty)";

        dispatch({
          type: "UPDATE_MESSAGE",
          id: thinkId,
          patch: {
            role: "assistant",
            content: answer,
            cited_nodes: (data?.cited_nodes as string[]) ?? [],
            alerts: (data?.alerts as ChatMessage["alerts"]) ?? [],
            verdict: (data?.verdict as string) ?? undefined,
            external_search: data?.external_search as ChatMessage["external_search"],
            context_usage: data?.context_usage as ChatMessage["context_usage"],
            sources: (data?.sources as ChatMessage["sources"]) ?? [],
            citations: (data?.citations as ChatMessage["citations"]) ?? [],
          },
        });

        dispatch({ type: "SET_BUSY", payload: false });

        // Refresh runs list
        api.runs.list(
          state.currentProjectId,
          getConversationSessionId(state.currentProjectId),
        ).then((runs) =>
          dispatch({ type: "SET_RUNS", payload: runs })
        ).catch(() => {});
      };

      source.addEventListener("done", (e: MessageEvent) => {
        finish(safeJson(e.data));
      });

      source.onerror = () => {
        finish(null);
      };
    },
    [state.busy, state.currentProjectId, dispatch, startTimer, stopTimer]
  );

  return { run, busy: state.busy };
}
