import { useState } from "react";
import { AppProvider } from "./store/AppContext";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { GraphPanel } from "./components/GraphPanel";
import { TracePanel } from "./components/TracePanel";
import { ToastContainer } from "./components/Sidebar";
import { useInit } from "./hooks/useInit";

function Inner() {
  useInit();
  const [activeView, setActiveView] = useState<"graph" | "chat">("graph");
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
            <span className="main-tab-hint">
              {activeView === "graph" ? "Explore project knowledge" : "Ask questions and inspect evidence"}
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
              id="chat-view-panel"
              className="main-view-slot"
              role="tabpanel"
              aria-labelledby="chat-view-tab"
              hidden={activeView !== "chat"}
            >
              <ChatPanel />
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
