export function defaultProcessFilter(processes, chosen) {
  return chosen || (processes.some((process) => process.status === "running") ? "running" : "all");
}

export function visibleProcesses(processes, filter, search) {
  const query = String(search || "").trim().toLowerCase();
  return processes.filter((process) => (filter !== "running" || process.status === "running")
    && (!query || [process.command, process.cwd, process.runtimeSessionID, process.processID]
      .some((value) => String(value ?? "").toLowerCase().includes(query))))
    .sort((a, b) => {
      const first = Date.parse(a.startedAt);
      const second = Date.parse(b.startedAt);
      // Keep server order for missing/equal times, never infer chronology from IDs.
      return Number.isFinite(first) && Number.isFinite(second) ? second - first : 0;
    });
}

export function selectedProcessDetail(detail, requestedID) {
  return detail && (!requestedID || detail.processID === requestedID) ? detail : null;
}

export function statusText(process) {
  if (process.status === "running") return "Running";
  if (process.status === "failed") return process.exitCode == null ? "Failed" : `Failed, exit ${process.exitCode}`;
  return process.exitCode == null ? "Exited" : `Exited, code ${process.exitCode}`;
}

export function singleLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim() || "Command";
}

export function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "Not reported";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function formatStarted(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString() : "Start time unavailable";
}

export function outputSummary(output) {
  if (!output) return "No retained output";
  return `${formatBytes(output.retainedBytes)} retained${output.truncated ? ", tail only" : ""}`;
}

export function outputText(process) {
  return process.output?.tail || (process.status === "running" ? "Waiting for process output…" : "No output.");
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(value >= 100 * 1024 ? 0 : 1)} KiB`;
  return `${value} B`;
}
