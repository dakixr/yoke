import { agentText, liveAgent } from "./model.js";

// Deep delegation stays readable; further nesting keeps the last indentation.
export const MAX_AGENT_DEPTH = 4;

export function defaultAgentFilter(rows, chosen) {
  return chosen || (rows.some((run) => run.status === "running") ? "running" : "all");
}

export function agentModel(run) {
  return [agentText(run.provider), agentText(run.model)].filter(Boolean).join(" / ");
}

export function agentName(run) {
  return agentText(run.name, agentModel(run) || "Unnamed agent");
}

export function activeAgentCount(state) {
  return state.current ? state.rows.filter(liveAgent).length : null;
}

/**
 * Filter and search keep the controller order, then place each child under a
 * shown parent. A child whose parent is hidden or no longer retained is a root.
 */
export function visibleAgents(rows, filter, search) {
  const query = String(search || "").trim().toLowerCase();
  const shown = rows.filter((run) => (filter !== "running" || run.status === "running")
    && (!query || [run.name, run.provider, run.model, run.agentId, run.runId, run.taskId, run.lastToolName, run.errorType]
      .some((value) => String(value ?? "").toLowerCase().includes(query))));
  const ids = new Set(shown.map((run) => run.agentId));
  const children = new Map();
  for (const run of shown) {
    const parent = ids.has(run.parentAgentId) && run.parentAgentId !== run.agentId ? run.parentAgentId : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(run);
  }
  const ordered = [];
  const placed = new Set();
  const visit = (run, depth) => {
    if (placed.has(run.agentId)) return;
    placed.add(run.agentId);
    ordered.push({ run, depth: Math.min(depth, MAX_AGENT_DEPTH) });
    for (const child of children.get(run.agentId) || []) visit(child, depth + 1);
  };
  for (const run of children.get(null) || []) visit(run, 0);
  // A parent cycle has no root. Show its members flat rather than dropping them.
  for (const run of shown) visit(run, 0);
  return ordered;
}

export function agentDurationMs(run, now = Date.now()) {
  const started = Date.parse(run.startedAt);
  const finished = run.finishedAt ? Date.parse(run.finishedAt) : run.status === "running" ? now : NaN;
  return Number.isFinite(started) && Number.isFinite(finished) ? Math.max(0, finished - started) : NaN;
}

export function agentChildren(rows, agentId) {
  return rows.filter((run) => run.parentAgentId === agentId && run.agentId !== agentId);
}
