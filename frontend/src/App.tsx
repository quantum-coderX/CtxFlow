import { useState } from "react";
import { AppProvider } from "./store/AppContext";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { GraphPanel } from "./components/GraphPanel";
import { MemoryPanel } from "./components/MemoryPanel";
import { TracePanel } from "./components/TracePanel";
import { DataPanel } from "./components/DataPanel";
import { ReportsPanel } from "./components/ReportsPanel";
import { ToastContainer } from "./components/Sidebar";
import { useInit } from "./hooks/useInit";

function Inner() {
  useInit();
  const [activeView, setActiveView] = useState<"graph" | "memory" | "chat" | "data" | "reports">("graph");
  return (
    <>
      <TopBar />
      <div className="workspace">
        <Sidebar />
        <main className="main-workspace">
          <nav className="main-tabs" role="tablist" aria-label="Workspace view">
            <button
              id="graph-view-tab"
              className={`main-tab ${activeView === "graph" ? "active" : ""}`}
              role="tab"
              aria-selected={activeView === "graph"}
              aria-controls="graph-view-panel"
              onClick={() => setActiveView("graph")}
            >
              <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden>
                <circle cx="4" cy="5" r="2" /><circle cx="15.5" cy="4" r="2" />
                <circle cx="10" cy="15.5" r="2.5" /><path d="m5.8 5.2 7.8-.8M5.2 6.6l3.6 6.7m5.4-7.5-3.1 8" />
              </svg>
              Graph
            </button>
            <button
              id="memory-view-tab"
              className={`main-tab ${activeView === "memory" ? "active" : ""}`}
              role="tab"
              aria-selected={activeView === "memory"}
              aria-controls="memory-view-panel"
              onClick={() => setActiveView("memory")}
            >
              Memory
            </button>
            <button
              id="chat-view-tab"
              className={`main-tab ${activeView === "chat" ? "active" : ""}`}
              role="tab"
              aria-selected={activeView === "chat"}
              aria-controls="chat-view-panel"
              onClick={() => setActiveView("chat")}
            >
              <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden>
                <path d="M3 4a2 2 0 012-2h10a2 2 0 012 2v8a2 2 0 01-2 2H9l-4 4v-4a2 2 0 01-2-2V4z" />
              </svg>
              Research chat
            </button>
            <button
              id="data-view-tab"
              className={`main-tab ${activeView === "data" ? "active" : ""}`}
              role="tab"
              aria-selected={activeView === "data"}
              aria-controls="data-view-panel"
              onClick={() => setActiveView("data")}
            >
              Data
            </button>
            <button
              id="reports-view-tab"
              className={`main-tab ${activeView === "reports" ? "active" : ""}`}
              role="tab"
              aria-selected={activeView === "reports"}
              aria-controls="reports-view-panel"
              onClick={() => setActiveView("reports")}
            >
              Reports
            </button>
            <span className="main-tab-hint">
              {activeView === "graph"
                ? "Explore project knowledge"
                : activeView === "memory"
                  ? "Browse durable memory by tier"
                  : activeView === "chat"
                    ? "Ask questions and inspect evidence"
                    : activeView === "data"
                      ? "Versioned datasets, fixed analyses and charts"
                      : "Cited, reproducible reports"}
            </span>
          </nav>
          <div className="main-view-stack">
            <div
              id="graph-view-panel"
              className="main-view-slot"
              role="tabpanel"
              aria-labelledby="graph-view-tab"
              hidden={activeView !== "graph"}
            >
              <GraphPanel />
            </div>
            <div
              id="memory-view-panel"
              className="main-view-slot"
              role="tabpanel"
              aria-labelledby="memory-view-tab"
              hidden={activeView !== "memory"}
            >
              <MemoryPanel />
            </div>
            <div
              id="chat-view-panel"
              className="main-view-slot"
              role="tabpanel"
              aria-labelledby="chat-view-tab"
              hidden={activeView !== "chat"}
            >
              <ChatPanel />
            </div>
            <div
              id="data-view-panel"
              className="main-view-slot"
              role="tabpanel"
              aria-labelledby="data-view-tab"
              hidden={activeView !== "data"}
            >
              {activeView === "data" && <DataPanel />}
            </div>
            <div
              id="reports-view-panel"
              className="main-view-slot"
              role="tabpanel"
              aria-labelledby="reports-view-tab"
              hidden={activeView !== "reports"}
            >
              {activeView === "reports" && <ReportsPanel />}
            </div>
          </div>
        </main>
        <TracePanel />
      </div>
      <ToastContainer />
    </>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Inner />
    </AppProvider>
  );
}
