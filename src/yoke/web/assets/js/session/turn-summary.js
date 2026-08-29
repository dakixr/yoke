// @ts-check

export function formatTurnSummary(summary) {
  const duration = Number(summary?.durationSeconds);
  const toolCount = Number(summary?.toolCount);
  if (!Number.isFinite(duration) || duration < 60) return "";
  let text = `Worked for ${formatTurnDuration(duration)}`;
  if (Number.isInteger(toolCount) && toolCount > 0) {
    text += ` · ${toolCount} tool${toolCount === 1 ? "" : "s"}`;
  }
  return text;
}

export function formatTurnDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.floor(seconds % 60);
  if (minutes < 60) return `${minutes}m${String(remaining).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h${String(minutes % 60).padStart(2, "0")}m`;
}
