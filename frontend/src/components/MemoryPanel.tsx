import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MemoryItem, MemoryTierFilter } from "../types";
import { useAppState } from "../store/AppContext";
import "../memory.css";

const TIERS: MemoryTierFilter[] = ["all", "hot", "warm", "cold"];

export function MemoryPanel() {
  const { currentProjectId: projectId } = useAppState();
  const [tier, setTier] = useState<MemoryTierFilter>("all");
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(
    async (next: string | null, replace: boolean) => {
      setLoading(true);
      setError("");
      try {
        const page = await api.memory.page(projectId, next, tier === "all" ? null : tier);
        setItems((previous) => (replace ? page.memories : [...previous, ...page.memories]));
        setCursor(page.next_cursor);
      } catch (reason: unknown) {
        setItems([]);
        setCursor(null);
        setError(reason instanceof Error ? reason.message : "Could not load memory.");
      } finally {
        setLoading(false);
      }
    },
    [projectId, tier]
  );

  useEffect(() => {
    void load(null, true);
  }, [load]);

  return (
    <section className="graph-panel" aria-label="Durable memory">
      <div className="graph-toolbar">
        <div className="graph-heading">
          <div className="graph-title">Durable memory</div>
          <div className="graph-subtitle">
            {items.length.toLocaleString()} loaded{cursor ? " · more available" : ""}
          </div>
        </div>
        <div className="graph-controls" role="group" aria-label="Storage tier">
          {TIERS.map((value) => (
            <button
              key={value}
              className={`main-tab ${tier === value ? "active" : ""}`}
              onClick={() => setTier(value)}
            >
              {value}
            </button>
          ))}
        </div>
      </div>
      <div className="memory-list">
        {error && <div className="graph-error">{error}</div>}
        {!error && !loading && items.length === 0 && (
          <div className="history-empty">No memories in this tier.</div>
        )}
        {items.map((item) => (
          <article key={item.id} className="memory-item">
            <div className="memory-meta">
              <span className="access-badge">{item.storage_tier}</span>
              <span className="access-badge">{item.memory_type}</span>
              <span className="access-badge">{item.validity}</span>
              {item.proposed_tier && item.proposed_tier !== item.storage_tier && (
                <span className="access-badge" title={item.tier_reason ?? ""}>
                  Laya: {item.proposed_tier}
                </span>
              )}
            </div>
            <p className="memory-text">{item.text}</p>
          </article>
        ))}
        {cursor && (
          <button className="btn" disabled={loading} onClick={() => void load(cursor, false)}>
            {loading ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    </section>
  );
}
