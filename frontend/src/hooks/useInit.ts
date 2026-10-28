import { useEffect, useRef } from "react";
import { useAppState, useDispatch } from "../store/AppContext";
import { api } from "../api";
import { getConversationSessionId } from "../session";
import type { ChatMessage } from "../types";

export function useInit() {
  const state = useAppState();
  const dispatch = useDispatch();
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    // Load health
    api.health()
      .then((h) => dispatch({ type: "SET_HEALTH", payload: h }))
      .catch(() => {});

    // Load projects
    api.projects.list()
      .then((projects) => dispatch({ type: "SET_PROJECTS", payload: projects }))
      .catch(() => {});

    // Poll health every 30s
    const interval = setInterval(() => {
      api.health()
        .then((h) => dispatch({ type: "SET_HEALTH", payload: h }))
        .catch(() => {});
    }, 30_000);

    return () => clearInterval(interval);
  }, [dispatch]);

  // Reload runs and the active conversation when the project changes.
  useEffect(() => {
    let active = true;
    const sessionId = getConversationSessionId(state.currentProjectId);
    api.runs.list(state.currentProjectId, sessionId)
      .then((runs) => {
        if (!active) return;
        dispatch({ type: "SET_RUNS", payload: runs });
        const messages: ChatMessage[] = [...runs]
          .sort((left, right) => left.started_at - right.started_at)
          .flatMap((run) => {
            const userMessage: ChatMessage = {
              id: `user-${run.run_id}`,
              role: "user",
              content: run.query,
              timestamp: run.started_at * 1000,
            };
            const assistantMessage: ChatMessage | null = run.answer
              ? {
                  id: `assistant-${run.run_id}`,
                  role: "assistant",
                  content: run.answer,
                  cited_nodes: run.cited_nodes,
                  alerts: run.alerts,
                  verdict: run.verdict,
                  context_usage: run.context_usage,
                  sources: run.sources,
                  citations: run.citations,
                  timestamp: (run.ended_at ?? run.started_at) * 1000,
                }
              : null;
            return assistantMessage
              ? [userMessage, assistantMessage]
              : [userMessage];
          });
        dispatch({ type: "RESTORE_CONVERSATION", payload: messages });
      })
      .catch((error: unknown) => console.error("Could not restore conversation history.", error));
    return () => {
      active = false;
    };
  }, [state.currentProjectId, dispatch]);
}
