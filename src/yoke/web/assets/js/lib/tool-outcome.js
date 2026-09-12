export function callOutcome(call) {
  const result = resultObject(call.result);
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

// Saved chat results are JSON text; live events and inspector calls use objects.
function resultObject(value) {
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return {};
    }
  }
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}
