import { callOutcome } from "./presenters.js";

export function toolSearchText(call) {
  return [call.toolName, call.id, call.status, call.arguments?.raw, call.arguments?.executed, call.result]
    .filter((value) => value != null)
    .map((value) => typeof value === "string" ? value : JSON.stringify(value))
    .join(" ").toLowerCase();
}

export function filterCalls(calls, search, status) {
  const query = search.trim().toLowerCase();
  return calls.filter((call) => {
    const state = callOutcome(call).state;
    const matchesStatus = !status || status === "all" || state === status || (status === "running" && state === "pending");
    return matchesStatus && (!query || toolSearchText(call).includes(query));
  });
}

export function loadedScope(loaded, total, matching, filtered) {
  const count = Number.isFinite(total)
    ? loaded <= total ? `${loaded} of ${total} calls loaded` : `${loaded} calls loaded; ${total} currently available`
    : `${loaded} calls loaded; total unavailable`;
  return filtered ? `${count}. ${matching} match loaded calls.` : count;
}

export function selectedOutsideWindow(calls, visible, callID) {
  if (!callID || visible.some((call) => call.id === callID)) return null;
  return calls.some((call) => call.id === callID) ? "Selected call is hidden by filters" : "Selected call is outside loaded history";
}

// Older pages prepend calls. Only calls after the last observed tail are new.
export function newCallCount(calls, lastObservedID, lastObservedSequence = null) {
  if (!lastObservedID) return 0;
  const index = calls.findIndex((call) => call.id === lastObservedID);
  if (index >= 0) return calls.length - index - 1;
  return Number.isFinite(lastObservedSequence)
    ? calls.filter((call) => Number.isFinite(call.sequence) && call.sequence > lastObservedSequence).length : 0;
}

export function nearBottom(node, tolerance = 36) {
  return node.scrollHeight - node.clientHeight - node.scrollTop <= tolerance;
}

export function listScrollPosition(node) {
  const top = node.getBoundingClientRect().top;
  const anchor = [...node.querySelectorAll("[data-call-id]")].find((row) => row.getBoundingClientRect().bottom > top);
  return {
    scrollTop: node.scrollTop,
    anchorID: anchor?.dataset.callId || null,
    anchorOffset: anchor ? anchor.getBoundingClientRect().top - top : 0,
  };
}

export function restoredScrollTop(node, saved, following) {
  if (following) return Math.max(0, node.scrollHeight - node.clientHeight);
  return Math.max(0, Math.min(saved || 0, node.scrollHeight - node.clientHeight));
}
