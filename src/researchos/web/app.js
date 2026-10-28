/* ResearchOS — Workflow-aware vanilla-JS frontend */

"use strict";

// ─── App State ──────────────────────────────────────────────────────────────

const state = {
  graph:         { nodes: [], edges: [] },
  selectedNode:  null,
  busy:          false,
  projects:      [],
  currentProject: "default",
  runs:          [],
  activeRunId:   null,
  stageStart:    0,
};

const STAGES = ["plan", "retrieve", "read", "memory", "pack", "synthesize", "verify", "persist"];

// ─── DOM Utilities ───────────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function toast(message, kind = "neutral") {
  const box = $("#toast");
  box.textContent = message;
  box.className = "toast show" + (kind === "bad" ? " bad" : kind === "good" ? " good" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => box.classList.remove("show"), 3600);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) throw new Error((body && body.detail) ? body.detail : `${res.status} ${res.statusText}`);
  return body;
}

function jsonPost(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function setBusy(btn, busy, label) {
  if (!btn) return;
  if (busy) { btn._label = btn.textContent; btn.disabled = true; if (label) btn.textContent = label; }
  else { if (btn._label) btn.textContent = btn._label; btn.disabled = false; }
}

// ─── Projects ────────────────────────────────────────────────────────────────

async function loadProjects() {
  try {
    const data = await api("/api/projects");
    state.projects = data.projects || [];
    renderProjectSelector();
  } catch (e) {
    console.warn("Projects unavailable:", e.message);
  }
}

function renderProjectSelector() {
  const sel = $("#project-select");
  const current = sel.value || state.currentProject;
  sel.innerHTML = "";
  for (const p of state.projects) {
    const opt = document.createElement("option");
    opt.value = p.project_id;
    opt.textContent = p.name ? `${p.project_id} (${p.name})` : p.project_id;
    if (p.project_id === current) opt.selected = true;
    sel.appendChild(opt);
  }
}

function currentProject() {
  return $("#project-select").value || "default";
}

// ─── Health ───────────────────────────────────────────────────────────────────

async function loadHealth() {
  const dot  = $("#health-dot");
  const lbl  = $("#health-label");
  const badge = $("#model-badge");
  try {
    const d = await api("/health");
    dot.className = "status-dot ok";
    lbl.textContent = `v${d.version} · online`;
    $("#stat-nodes").textContent = d.graph_nodes;
    $("#stat-edges").textContent = d.graph_edges;
    if (d.model_enabled && d.model_provider) {
      badge.hidden = false;
      $("#model-badge-text").textContent = (d.model_provider || "").toUpperCase();
    } else {
      badge.hidden = true;
    }
  } catch (_) {
    dot.className = "status-dot bad";
    lbl.textContent = "offline";
    badge.hidden = true;
  }
}

// ─── Graph Data ───────────────────────────────────────────────────────────────

async function loadGraph() {
  try {
    state.graph = await api(`/api/graph/data?project_id=${encodeURIComponent(currentProject())}`);
    renderGraph();
    renderPapers();
    updateGraphStats();
  } catch (e) {
    toast(`Graph: ${e.message}`, "bad");
  }
}

function updateGraphStats() {
  const n = state.graph.nodes.length;
  const e = state.graph.edges.length;
  $("#stat-nodes").textContent = n;
  $("#stat-edges").textContent = e;
}

// ─── Papers List ─────────────────────────────────────────────────────────────

function renderPapers() {
  const list  = $("#papers-list");
  const papers = state.graph.nodes.filter(n => n.node_type === "paper");
  list.replaceChildren();
  if (!papers.length) {
    list.append(el("li", "empty-state", "No papers ingested yet."));
    return;
  }
  for (const p of papers.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))) {
    const title = (p.metadata && p.metadata.title) || p.summary || p.content?.slice(0, 60) || "Untitled";
    const year  = (p.metadata && p.metadata.year) || "";
    const li  = el("li");
    li.append(el("span", "item-title", title));
    if (year) li.append(el("span", "item-meta", String(year)));
    li.addEventListener("click", () => showTrace(p.id));
    list.append(li);
  }
}

// ─── Notes ────────────────────────────────────────────────────────────────────

async function loadNotes() {
  const list = $("#notes-list");
  try {
    const data = await api(`/api/notes?project_id=${encodeURIComponent(currentProject())}`);
    list.replaceChildren();
    if (!data.notes.length) { list.append(el("li", "empty-state", "No notes yet.")); return; }
    for (const note of data.notes) {
      const li = el("li");
      li.append(el("span", "item-title", note.title || "Note"));
      li.append(el("span", "item-meta", note.content.slice(0, 60)));
      list.append(li);
    }
  } catch (e) {
    list.replaceChildren(el("li", "empty-state", `Could not load notes: ${e.message}`));
  }
}

// ─── Runs History ─────────────────────────────────────────────────────────────

async function loadRuns() {
  const list = $("#runs-list");
  try {
    const data = await api(`/api/runs?project_id=${encodeURIComponent(currentProject())}&limit=20`);
    state.runs = data.runs || [];
    renderRunHistory();
  } catch (e) {
    list.replaceChildren(el("li", "empty-state", "Run history unavailable."));
  }
}

function renderRunHistory() {
  const list = $("#runs-list");
  list.replaceChildren();
  if (!state.runs.length) {
    list.append(el("li", "empty-state", "No runs yet. Ask a question to start."));
    return;
  }
  for (const run of state.runs) {
    const li = el("li");
    li.dataset.runId = run.run_id;
    const queryEl = el("span", "run-query", run.query || "(no query)");
    const meta = el("span", "run-meta");
    const verdict = run.verdict || "";
    if (verdict) {
      const vb = el("span", `run-verdict verdict-${verdict}`, verdict.replace(/_/g, " "));
      meta.append(vb);
    }
    const status = el("span", "", run.status || "");
    meta.append(status);
    li.append(queryEl, meta);
    li.addEventListener("click", () => loadRunDetail(run.run_id));
    list.append(li);
  }
}

async function loadRunDetail(runId) {
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    showRunInChat(run);
  } catch (e) {
    toast(`Could not load run: ${e.message}`, "bad");
  }
}

function showRunInChat(run) {
  if (!run.answer) return;
  appendMessage("user",      run.query || "(run)");
  appendMessage("assistant", run.answer, {
    cited_nodes: run.cited_nodes,
    alerts:      run.alerts,
    verdict:     run.verdict,
    external_search: run.external_search,
    context_usage:   run.context_usage,
  });
}

// ─── Workflow SSE ─────────────────────────────────────────────────────────────

let _activeSource = null;

function runQuery(query) {
  if (state.busy) return;
  state.busy = true;
  state.stageStart = Date.now();

  const budget  = parseInt($("#token-budget").value || "2048", 10);
  const project = currentProject();
  const params  = new URLSearchParams({ query, project_id: project, token_budget: budget });
  const url     = `/api/stream?${params}`;

  // Show user message
  appendMessage("user", query);

  // Show workflow bar
  const bar = $("#workflow-bar");
  bar.hidden = false;
  renderWorkflowStages({});
  startElapsedTimer();

  // Placeholder assistant message
  const thinkingId = appendThinking();

  let finalData = null;
  const stageStates = {};

  if (_activeSource) { try { _activeSource.close(); } catch (_) {} }
  const source = new EventSource(url);
  _activeSource = source;

  source.addEventListener("start", (e) => {
    const d = safeJson(e.data);
    state.activeRunId = d.run_id || null;
    stageStates["_start"] = "started";
    renderWorkflowStages(stageStates);
  });

  for (const stage of STAGES) {
    source.addEventListener(stage, (e) => {
      const d = safeJson(e.data);
      stageStates[stage] = d.status || "started";
      renderWorkflowStages(stageStates);
    });
  }

  source.addEventListener("done", (e) => {
    finalData = safeJson(e.data);
    source.close();
    _activeSource = null;
    finishRun(thinkingId, finalData, stageStates);
  });

  source.onerror = (e) => {
    source.close();
    _activeSource = null;
    if (!finalData) {
      replaceThinking(thinkingId, "The request encountered an error. Please check that the server is running.");
    }
    finishRun(thinkingId, finalData, stageStates);
  };
}

function finishRun(thinkingId, data, stageStates) {
  stopElapsedTimer();
  state.busy = false;
  setBusy($("#send-btn"), false);

  if (!data) {
    replaceThinking(thinkingId, "No response received. Check server status.");
    markAllStagesDone(stageStates, "failed");
    renderWorkflowStages(stageStates);
    return;
  }

  const answer = data.answer || "(No answer generated — graph may be empty)";
  replaceThinkingWithAnswer(thinkingId, answer, data);

  for (const s of STAGES) {
    if (!stageStates[s]) stageStates[s] = "done";
    else if (stageStates[s] === "started") stageStates[s] = "done";
  }
  renderWorkflowStages(stageStates);

  // Update graph & stats
  loadGraph();
  loadRuns();

  // Context usage
  if (data.context_usage && data.context_usage.token_budget) {
    showContextUsage(data.context_usage);
  }
}

function markAllStagesDone(obj, status) {
  for (const s of STAGES) { if (!obj[s]) obj[s] = status; }
}

// ─── Message Rendering ────────────────────────────────────────────────────────

function appendMessage(role, text, extra = {}) {
  const msgs = $("#messages");
  const art  = el("article", `message ${role}`);
  const av   = el("div", "message-avatar", role === "assistant" ? "R" : "U");
  const body = el("div", "message-body");
  body.innerHTML = renderMarkdown(text);

  if (extra.cited_nodes && extra.cited_nodes.length) {
    const citBar = el("div", "message-alerts");
    for (const nodeId of extra.cited_nodes.slice(0, 8)) {
      const link = el("a", "citation-link", `[Node:${nodeId}]`);
      link.href = "#";
      link.addEventListener("click", (e) => { e.preventDefault(); showTrace(nodeId); });
      citBar.append(link);
    }
    body.append(citBar);
  }

  if (extra.alerts && extra.alerts.length) {
    const alertBar = el("div", "message-alerts");
    for (const alert of extra.alerts.slice(0, 4)) {
      const cls = alert.type === "superseded"   ? "alert-item alert-superseded"
                : alert.type === "contradiction" ? "alert-item alert-contradiction"
                :                                  "alert-item alert-guardrail";
      alertBar.append(el("div", cls, alert.message || alert.type));
    }
    body.append(alertBar);
  }

  if (extra.verdict) {
    const vb = el("div", `verdict-badge verdict-${extra.verdict}`, extra.verdict.replace(/_/g, " "));
    body.append(vb);
  }

  if (extra.external_search) {
    const es = extra.external_search;
    const cls = es.status === "ok" ? "external-search-note ok" : "external-search-note err";
    const note = el("div", cls,
      es.status === "ok"
        ? `Literature search: found results (${es.provider || "external"})`
        : `Literature search: ${es.status} — ${Object.values(es.errors || {}).join("; ")}`
    );
    body.append(note);
  }

  art.append(av, body);
  msgs.append(art);
  msgs.scrollTop = msgs.scrollHeight;
  return art;
}

function appendThinking() {
  const msgs = $("#messages");
  const id   = `thinking-${Date.now()}`;
  const art  = el("article", "message assistant thinking");
  art.id = id;
  const av   = el("div", "message-avatar", "R");
  const body = el("div", "message-body");
  body.innerHTML = `<span class="thinking-dots"><span></span><span></span><span></span></span>`;
  art.append(av, body);
  msgs.append(art);
  msgs.scrollTop = msgs.scrollHeight;
  return id;
}

function replaceThinking(id, text) {
  const art = document.getElementById(id);
  if (!art) return;
  art.classList.remove("thinking");
  const body = art.querySelector(".message-body");
  if (body) body.innerHTML = renderMarkdown(text);
}

function replaceThinkingWithAnswer(id, answer, data) {
  const art = document.getElementById(id);
  if (!art) return;
  art.classList.remove("thinking");
  const body = art.querySelector(".message-body");
  if (!body) return;
  body.innerHTML = renderMarkdown(answer);

  // Make node IDs in answer clickable
  body.querySelectorAll("code").forEach(code => {
    const match = code.textContent.match(/^Node:(.+)$/);
    if (match) {
      const link = el("a", "citation-link", code.textContent);
      link.href = "#";
      link.addEventListener("click", e => { e.preventDefault(); showTrace(match[1]); });
      code.replaceWith(link);
    }
  });

  // Post-process [Node:XXX] patterns in text nodes
  processNodeLinks(body);

  // Citations, alerts, verdict
  if (data.cited_nodes && data.cited_nodes.length) {
    const bar = el("div", "message-alerts");
    for (const nid of data.cited_nodes.slice(0, 8)) {
      const lnk = el("a", "citation-link", `[Node:${nid.slice(0, 12)}…]`);
      lnk.href = "#";
      lnk.title = nid;
      lnk.addEventListener("click", e => { e.preventDefault(); showTrace(nid); });
      bar.append(lnk);
    }
    body.append(bar);
  }

  if (data.alerts && data.alerts.length) {
    const aBar = el("div", "message-alerts");
    for (const alert of data.alerts.slice(0, 4)) {
      const cls = alert.type === "superseded" ? "alert-item alert-superseded"
                : alert.type === "contradiction" ? "alert-item alert-contradiction"
                : "alert-item alert-guardrail";
      aBar.append(el("div", cls, alert.message || alert.type));
    }
    body.append(aBar);
  }

  if (data.verdict) {
    body.append(el("div", `verdict-badge verdict-${data.verdict}`, data.verdict.replace(/_/g, " ")));
  }

  if (data.external_search) {
    const es = data.external_search;
    const cls = es.status === "ok" ? "external-search-note ok" : "external-search-note err";
    body.append(el("div", cls,
      es.status === "ok"
        ? `Literature search: found results`
        : `Literature search: ${es.status}`
    ));
  }

  $("#messages").scrollTop = $("#messages").scrollHeight;
}

function processNodeLinks(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes  = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const tn of nodes) {
    if (!tn.textContent.includes("[Node:")) continue;
    const parent = tn.parentNode;
    const frag   = document.createDocumentFragment();
    let rest = tn.textContent;
    const pat = /\[Node:([^\]]+)\]/g;
    let m, last = 0;
    while ((m = pat.exec(rest)) !== null) {
      frag.append(document.createTextNode(rest.slice(last, m.index)));
      const lnk = el("a", "citation-link", m[0]);
      lnk.href = "#";
      const nid = m[1];
      lnk.addEventListener("click", e => { e.preventDefault(); showTrace(nid); });
      frag.append(lnk);
      last = pat.lastIndex;
    }
    frag.append(document.createTextNode(rest.slice(last)));
    parent.replaceChild(frag, tn);
  }
}

function renderMarkdown(text) {
  // Minimal markdown: bold, italic, code, newlines
  return String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

// ─── Workflow Stages Bar ──────────────────────────────────────────────────────

function renderWorkflowStages(stageMap) {
  const container = $("#workflow-stages");
  container.replaceChildren();
  for (const stage of STAGES) {
    const st = stageMap[stage] || "pending";
    const cls = st === "started"  ? "stage-pill active"
              : st === "completed"|| st === "done"    ? "stage-pill done"
              : st === "degraded" ? "stage-pill degraded"
              : st === "failed"   ? "stage-pill failed"
              : st === "skipped"  ? "stage-pill done"
              : st === "blocked"  ? "stage-pill failed"
              :                    "stage-pill";
    const pill = el("div", cls);
    pill.setAttribute("role", "listitem");
    pill.setAttribute("title", `${stage}: ${st}`);
    const dot = el("span", "stage-dot");
    pill.append(dot, document.createTextNode(stage));
    container.append(pill);
  }
}

let _elapsedTimer = null;

function startElapsedTimer() {
  stopElapsedTimer();
  const el = $("#workflow-elapsed");
  _elapsedTimer = setInterval(() => {
    const s = ((Date.now() - state.stageStart) / 1000).toFixed(1);
    if (el) el.textContent = `${s}s`;
  }, 200);
}

function stopElapsedTimer() {
  if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null; }
}

// ─── Context Usage ────────────────────────────────────────────────────────────

function showContextUsage(usage) {
  const strip = $("#context-usage-strip");
  if (!strip) return;
  strip.hidden = false;
  const used   = usage.nodes_used   || 0;
  const budget = usage.token_budget || 2048;
  const tokens = usage.tokens_used  || null;
  const pct    = tokens ? Math.min(100, Math.round((tokens / budget) * 100)) : 0;
  const fill   = $("#usage-bar-fill");
  const label  = $("#usage-label");
  const detail = $("#usage-detail");
  if (fill)  fill.style.width = `${pct}%`;
  if (label) label.textContent = tokens ? `${tokens.toLocaleString()} / ${budget.toLocaleString()} tokens` : `${used} node(s) used`;
  if (detail) detail.textContent = `${usage.nodes_retrieved || 0} retrieved · ${used} selected · ${usage.nodes_dropped || 0} dropped`;
}

// ─── Research Trace ───────────────────────────────────────────────────────────

async function showTrace(nodeId) {
  const empty   = $("#evidence-empty");
  const content = $("#evidence-content");
  const closeBtn= $("#close-evidence-btn");

  empty.hidden   = true;
  content.hidden = false;
  if (closeBtn) closeBtn.hidden = false;

  $("#trace-node-id").textContent = nodeId;
  $("#trace-node-content").textContent = "Loading…";

  const node = state.graph.nodes.find(n => n.id === nodeId);
  if (node) {
    const typeBadge   = $("#trace-node-type");
    const statusBadge = $("#trace-node-status");
    if (typeBadge) { typeBadge.textContent = node.node_type; typeBadge.className = "node-type-badge"; }
    if (statusBadge) {
      const s = typeof node.status === "string" ? node.status : "active";
      statusBadge.textContent = s;
      statusBadge.className = `node-status-badge ${s}`;
    }
    $("#trace-node-content").textContent = node.content || node.summary || "(no content)";
  }

  try {
    const trace = await api(`/api/trace?project_id=${encodeURIComponent(currentProject())}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId }),
    });

    renderTraceList("#supporting-list",      trace.provenance_chain,  renderChainItem);
    renderTraceList("#superseded-list",      trace.superseded_claims, renderNodeItem);
    renderTraceList("#contradictions-list",  trace.contradictions,    renderContradictionItem);
    renderTraceList("#sources-list",         trace.source_papers,     renderNodeItem);
  } catch (e) {
    $("#trace-node-content").textContent = (node && (node.content || node.summary)) || "(could not load trace)";
    renderTraceList("#supporting-list", [], null);
  }

  state.selectedNode = nodeId;
  highlightGraphNode(nodeId);
}

function renderTraceList(sel, items, renderFn) {
  const list = $(sel);
  if (!list) return;
  list.replaceChildren();
  if (!items || !items.length) {
    list.append(el("li", "empty-state", "None"));
    return;
  }
  for (const item of items) {
    list.append(renderFn ? renderFn(item) : el("li", "", JSON.stringify(item)));
  }
}

function renderChainItem(step) {
  const li = el("li");
  if (step.edge_relation) li.append(el("div", "trace-relation", step.edge_relation));
  li.append(el("div", "trace-content", (step.content || "").slice(0, 200)));
  return li;
}

function renderNodeItem(node) {
  const li = el("li");
  li.append(el("div", "trace-content", (node.content || node.summary || "").slice(0, 200)));
  li.style.cursor = "pointer";
  li.addEventListener("click", () => showTrace(node.id));
  return li;
}

function renderContradictionItem(c) {
  const li = el("li");
  const rel = el("div", "trace-relation", `contradicts via ${c.relation || ""}`);
  const cnt = el("div", "trace-content", (c.content || "").slice(0, 200));
  li.append(rel, cnt);
  return li;
}

// ─── Tabs ────────────────────────────────────────────────────────────────────

function initTabs() {
  document.querySelectorAll(".evidence-tabs .tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => { b.classList.remove("active"); b.setAttribute("aria-selected", "false"); });
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      const panel = document.getElementById(`panel-${btn.dataset.tab}`);
      if (panel) panel.classList.add("active");
    });
  });
}

// ─── Graph Visualization ──────────────────────────────────────────────────────

const NODE_COLORS = {
  paper:      "var(--node-paper)",
  claim:      "var(--node-claim)",
  method:     "var(--node-method)",
  result:     "var(--node-result)",
  finding:    "var(--node-finding)",
  hypothesis: "var(--node-finding)",
  note:       "var(--node-note)",
  generic:    "var(--node-generic)",
};

function nodeColor(node) {
  const s = typeof node.status === "string" ? node.status : "active";
  if (s === "superseded") return "var(--node-superseded)";
  return NODE_COLORS[node.node_type] || NODE_COLORS.generic;
}

function renderGraph() {
  const svg    = $("#graph");
  const width  = Math.max(svg.clientWidth || 320, 280);
  const height = 200;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.replaceChildren();

  const { nodes, edges } = state.graph;
  if (!nodes.length) {
    const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
    txt.setAttribute("x", String(width / 2));
    txt.setAttribute("y", String(height / 2));
    txt.setAttribute("text-anchor", "middle");
    txt.setAttribute("fill", "var(--text-hint)");
    txt.setAttribute("font-size", "11");
    txt.textContent = "Ingest a paper to build the graph";
    svg.append(txt);
    return;
  }

  const positions = forceLayout(nodes, edges, width, height);
  const degree = new Map();
  for (const e of edges) {
    degree.set(e.source_id, (degree.get(e.source_id) || 0) + 1);
    degree.set(e.target_id, (degree.get(e.target_id) || 0) + 1);
  }

  const NS = "http://www.w3.org/2000/svg";

  // Edges
  for (const edge of edges) {
    const a = positions.get(edge.source_id);
    const b = positions.get(edge.target_id);
    if (!a || !b) continue;
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    line.setAttribute("class", `edge ${edge.relation_type || ""}`);
    svg.append(line);
  }

  // Nodes
  for (const node of nodes) {
    const p = positions.get(node.id);
    const r = 3.5 + Math.min(4, degree.get(node.id) || 0);
    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("cx", p.x); circle.setAttribute("cy", p.y); circle.setAttribute("r", r);
    circle.setAttribute("class", "node");
    circle.setAttribute("fill", nodeColor(node));
    circle.setAttribute("data-node-id", node.id);
    circle.setAttribute("title", `${node.node_type} · ${typeof node.status === "string" ? node.status : "active"}`);
    if (node.id === state.selectedNode) {
      circle.setAttribute("stroke", "var(--accent)");
      circle.setAttribute("stroke-width", "2");
    }
    circle.addEventListener("click", () => showTrace(node.id));
    svg.append(circle);

    const words = (node.summary || node.content || node.node_type || "").split(/\s+/);
    const label = document.createElementNS(NS, "text");
    label.setAttribute("x", p.x); label.setAttribute("y", p.y + r + 8);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "label");
    label.textContent = words.slice(0, 2).join(" ").slice(0, 22);
    svg.append(label);
  }
}

function highlightGraphNode(nodeId) {
  document.querySelectorAll("#graph .node").forEach(c => {
    c.removeAttribute("stroke");
    c.removeAttribute("stroke-width");
    if (c.getAttribute("data-node-id") === nodeId) {
      c.setAttribute("stroke", "var(--accent)");
      c.setAttribute("stroke-width", "2");
    }
  });
}

function forceLayout(nodes, edges, width, height) {
  const positions = new Map();
  const r = Math.min(width, height) * 0.36;
  nodes.forEach((node, i) => {
    const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
    positions.set(node.id, { x: width / 2 + Math.cos(angle) * r, y: height / 2 + Math.sin(angle) * r, vx: 0, vy: 0 });
  });

  const iters = nodes.length > 80 ? 60 : 140;
  for (let t = 0; t < iters; t++) {
    const cool = 1 - t / iters;
    for (let i = 0; i < nodes.length; i++) {
      const a = positions.get(nodes[i].id);
      for (let j = i + 1; j < nodes.length; j++) {
        const b = positions.get(nodes[j].id);
        let dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (2000 * cool) / (dist * dist);
        dx = (dx / dist) * f; dy = (dy / dist) * f;
        a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
      }
    }
    for (const edge of edges) {
      const a = positions.get(edge.source_id), b = positions.get(edge.target_id);
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (dist - 80) * 0.007 * cool;
      a.vx += (dx / dist) * f; a.vy += (dy / dist) * f;
      b.vx -= (dx / dist) * f; b.vy -= (dy / dist) * f;
    }
    for (const node of nodes) {
      const p = positions.get(node.id);
      p.vx += (width / 2 - p.x) * 0.003; p.vy += (height / 2 - p.y) * 0.003;
      p.vx = Math.max(-10, Math.min(10, p.vx)); p.vy = Math.max(-10, Math.min(10, p.vy));
      p.x = Math.max(12, Math.min(width - 12, p.x + p.vx));
      p.y = Math.max(12, Math.min(height - 12, p.y + p.vy));
      p.vx *= 0.74; p.vy *= 0.74;
    }
  }
  return positions;
}

// ─── Project Dialogs ──────────────────────────────────────────────────────────

function openNewProjectDialog() {
  const overlay = $("#new-project-overlay");
  overlay.hidden = false;
  $("#new-project-id").focus();
}

function closeNewProjectDialog() {
  $("#new-project-overlay").hidden = true;
  $("#new-project-form").reset();
}

// ─── Form Handlers ────────────────────────────────────────────────────────────

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.busy) return;
  const inp = $("#chat-input");
  const q   = inp.value.trim();
  if (!q) return;
  inp.value = "";
  inp.style.height = "";
  runQuery(q);
});

$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").dispatchEvent(new Event("submit", { cancelable: true }));
  }
});

// Auto-resize textarea
$("#chat-input").addEventListener("input", function () {
  this.style.height = "";
  this.style.height = Math.min(this.scrollHeight, 160) + "px";
});

$("#paper-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  setBusy(btn, true, "Ingesting…");
  const fd = new FormData(e.target);
  const authors = (fd.get("authors") || "").split(",").map(a => a.trim()).filter(Boolean);
  try {
    const res = await jsonPost("/api/papers/ingest", {
      project_id: currentProject(),
      title:    fd.get("title"),
      abstract: fd.get("abstract"),
      authors,
      year:     fd.get("year") ? parseInt(fd.get("year"), 10) : null,
      doi:      fd.get("doi") || "",
    });
    toast(`Ingested "${res.paper_node_id?.slice(0, 8)}" · ${res.claim_node_ids.length} claims`, "good");
    e.target.reset();
    await loadGraph();
  } catch (err) { toast(err.message, "bad"); }
  setBusy(btn, false);
});

$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  setBusy(btn, true, "Parsing…");
  const fd = new FormData(e.target);
  fd.append("project_id", currentProject());
  try {
    const res = await api("/api/papers/upload", { method: "POST", body: fd });
    toast(`Ingested PDF · ${res.claim_node_ids.length} claims`, "good");
    e.target.reset();
    await loadGraph();
  } catch (err) { toast(err.message, "bad"); }
  setBusy(btn, false);
});

$("#note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  setBusy(btn, true, "Saving…");
  const fd = new FormData(e.target);
  try {
    await jsonPost("/api/notes", {
      project_id: currentProject(),
      title:   fd.get("title"),
      content: fd.get("content"),
    });
    toast("Note saved", "good");
    e.target.reset();
    await loadNotes();
  } catch (err) { toast(err.message, "bad"); }
  setBusy(btn, false);
});

$("#demo-btn").addEventListener("click", async () => {
  const btn = $("#demo-btn");
  setBusy(btn, true);
  try {
    const res = await jsonPost("/api/demo/seed", { project_id: currentProject() });
    toast(`Demo corpus: ${res.papers_ingested} papers, ${res.claims_ingested} claims`, "good");
    await loadGraph();
  } catch (err) { toast(err.message, "bad"); }
  setBusy(btn, false);
});

$("#refresh-btn").addEventListener("click", async () => {
  await Promise.all([loadGraph(), loadNotes(), loadHealth()]);
  toast("Refreshed");
});

$("#reload-runs-btn").addEventListener("click", loadRuns);

// New project dialog
$("#new-project-btn").addEventListener("click", openNewProjectDialog);
$("#cancel-project-btn").addEventListener("click", closeNewProjectDialog);
$("#new-project-overlay").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeNewProjectDialog();
});

$("#new-project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  setBusy(btn, true, "Creating…");
  const fd = new FormData(e.target);
  try {
    await jsonPost("/api/projects", {
      project_id:  fd.get("project_id"),
      name:        fd.get("name"),
      description: fd.get("description"),
    });
    await loadProjects();
    const sel = $("#project-select");
    sel.value = fd.get("project_id");
    state.currentProject = fd.get("project_id");
    closeNewProjectDialog();
    toast("Project created", "good");
    await loadGraph();
  } catch (err) { toast(err.message, "bad"); }
  setBusy(btn, false);
});

// Project switch
$("#project-select").addEventListener("change", async () => {
  state.currentProject = currentProject();
  await Promise.all([loadGraph(), loadNotes(), loadRuns(), loadHealth()]);
});

// Close evidence panel
$("#close-evidence-btn").addEventListener("click", () => {
  $("#evidence-content").hidden = true;
  $("#evidence-empty").hidden = false;
  $("#close-evidence-btn").hidden = true;
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function safeJson(str) {
  try { return JSON.parse(str); } catch (_) { return {}; }
}

// ─── Bootstrap ───────────────────────────────────────────────────────────────

async function init() {
  initTabs();
  await loadHealth();
  await loadProjects();
  await loadGraph();
  await Promise.all([loadNotes(), loadRuns()]);

  // Poll health every 30s
  setInterval(loadHealth, 30_000);
}

init().catch(console.error);
