// Keep recent inactive agents, without hiding genuinely live work.
export const INACTIVE_AGENT_LIMIT = 100;
const statuses = new Set(["running", "completed", "failed", "cancelled", "interrupted"]);
const observations = new Set(["live", "stale", "lost"]);

function validRun(run, sessionID) {
  return run?.schemaVersion === 1 && run.sessionID === sessionID
    && typeof run.agentId === "string" && run.agentId.length > 0
    && typeof run.runId === "string" && run.runId.length > 0
    && Number.isSafeInteger(run.version) && run.version >= 0
    && statuses.has(run.status) && observations.has(run.observation)
    && typeof run.startedAt === "string"
    && Number.isFinite(Date.parse(run.startedAt));
}

function compareStarted(a, b) {
  const millis = Date.parse(a) - Date.parse(b);
  if (millis) return millis;
  // Python timestamps retain microseconds, including multiple starts per ms.
  const fraction = (value) => (value.match(/\.(\d+)(?:Z|[+-]\d\d:\d\d)$/)?.[1] || "").padEnd(9, "0").slice(3, 9);
  return fraction(a).localeCompare(fraction(b));
}

function newerRun(candidate, current) {
  if (candidate.runId !== current.runId) {
    const started = compareStarted(candidate.startedAt, current.startedAt);
    // A late update to an older run must not replace the next activation.
    if (started) return started > 0;
  }
  return candidate.version > current.version;
}

export function liveAgent(run) {
  return run.status === "running" && run.observation === "live";
}

export function reconcileAgentRuns(previous, snapshots, sessionID) {
  const latest = new Map();
  for (const run of snapshots) {
    if (!validRun(run, sessionID)) continue;
    const current = latest.get(run.agentId);
    if (!current || newerRun(run, current)) latest.set(run.agentId, run);
  }
  // The endpoint returns a full snapshot. Absent agents are no longer retained.
  for (const current of previous) {
    const incoming = latest.get(current.agentId);
    if (incoming && !newerRun(incoming, current)) latest.set(current.agentId, current);
  }
  let inactive = 0;
  const rows = [...latest.values()].sort((a, b) =>
    Number(liveAgent(b)) - Number(liveAgent(a))
      || compareStarted(b.startedAt, a.startedAt)
      || b.version - a.version,
  ).filter((run) => liveAgent(run) || ++inactive <= INACTIVE_AGENT_LIMIT);
  return rows.length === previous.length && rows.every((run, index) => run === previous[index])
    ? previous : rows;
}

export function agentPresentation(run, current = true) {
  const observation = !current && run.observation === "live" && run.status === "running"
    ? "stale" : run.observation;
  const labels = {
    running: "Running", completed: "Completed", failed: "Failed",
    cancelled: "Cancelled", interrupted: "Interrupted",
  };
  if (observation !== "live") {
    const label = observation === "lost" ? "Observation lost" : "Stale observation";
    return {
      label: run.status === "running" ? label : `${labels[run.status]} · ${label.toLowerCase()}`,
      tone: run.status === "failed" ? "failed" : "uncertain",
      detail: run.status === "running" ? "Execution state unknown." : null,
    };
  }
  return { label: labels[run.status], tone: run.status, detail: null };
}

export function agentText(value, fallback = "") {
  return typeof value === "string" && value ? value : fallback;
}
