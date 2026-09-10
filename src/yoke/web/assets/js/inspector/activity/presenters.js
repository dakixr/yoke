import { isPlainObject } from "../tool-logic.js";

export function callOutcome(call) {
  const result = isPlainObject(call.result) ? call.result : {};
  const exit = result.exit_code ?? result.exitCode ?? result.returncode;
  const parsedExit = typeof exit === "string" && /^-?\d+$/.test(exit) ? Number(exit) : exit;
  const exitCode = typeof parsedExit === "number" && Number.isFinite(parsedExit) ? parsedExit : null;
  const searchName = String(call.toolName || "").split(/[.:/]/).at(-1);
  const noMatches = ["rg", "grep", "fd"].includes(searchName) && exitCode === 1 && result.ok !== false;
  if (call.status === "cancelled" || result.cancelled === true) return { state: "cancelled", tone: "attention", label: "Cancelled", exitCode };
  if (["failed", "error"].includes(call.status) || result.ok === false || result.isError === true || result.timed_out === true || (exitCode !== null && exitCode !== 0 && !noMatches)) {
    return { state: "failed", tone: "error", label: result.timed_out === true ? "Timed out" : "Failed", exitCode };
  }
  if (call.status === "pending") return { state: "pending", tone: "attention", label: "Pending", exitCode };
  if (call.status === "running") return { state: "running", tone: "attention", label: "Running", exitCode };
  if (["ok", "completed", "success"].includes(call.status)) return { state: "completed", tone: "neutral", label: noMatches ? "No matches" : "Completed", exitCode };
  return { state: "unknown", tone: "neutral", label: "Unknown", exitCode };
}

export function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "duration unavailable";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  return `${Math.floor(seconds / 60)}m ${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
}

export function formatTimestamp(value) {
  if (!value) return "time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "time unavailable";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}
