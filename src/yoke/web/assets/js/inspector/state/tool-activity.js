import { ApiError, api } from "../../api/client.js";
import { store } from "../../state/store.js";

const PAGE_SIZE = 100;

/** Keep one chronological history window independent of detail selection. */
export class ToolActivityState {
  constructor(owner) {
    this.owner = owner;
    this.detailRequests = new Map();
  }

  invalidate() { this.detailRequests.clear(); }

  async list(sessionID, { replace = false } = {}) {
    const owner = this.owner;
    const request = owner.nextRequest(sessionID, "tool:list");
    const selection = owner.selectedRequest(sessionID, "tool");
    const response = await api.toolCalls(sessionID, { order: "latest", limit: PAGE_SIZE });
    if (!owner.ownsRequest(request) || !owner.ownsSelection(sessionID, "tool", selection)) return null;
    store.setState((state) => {
      const data = state.sessionData[sessionID] || {};
      const merged = mergeLatestToolWindow(data.toolCalls, response.data, replace);
      return {
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...data,
            toolCalls: merged.calls,
            toolCallsCursor: merged.keptOlder ? data.toolCallsCursor : response.cursor,
            toolCallsTotal: response.total ?? merged.calls.length,
            toolCallsWindowChanged: merged.replacedOlder,
          },
        },
      };
    });
    return response.data;
  }

  async loadOlder(sessionID) {
    const owner = this.owner;
    const cursor = store.getState().sessionData[sessionID]?.toolCallsCursor?.next;
    if (!cursor) return null;
    const request = owner.nextRequest(sessionID, "tool:older");
    const selection = owner.selectedRequest(sessionID, "tool");
    try {
      const response = await api.toolCalls(sessionID, { order: "latest", limit: PAGE_SIZE, cursor });
      if (!owner.ownsRequest(request) || !owner.ownsSelection(sessionID, "tool", selection)) return null;
      const current = store.getState().sessionData[sessionID];
      if (current?.toolCallsCursor?.next !== cursor) return null;
      const ids = new Set((current.toolCalls || []).map((call) => call.id));
      const older = (response.data || []).filter((call) => !ids.has(call.id));
      store.setState((state) => ({
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...state.sessionData[sessionID],
            toolCalls: [...older, ...(state.sessionData[sessionID]?.toolCalls || [])],
            toolCallsCursor: response.cursor,
            toolCallsTotal: Math.max(response.total || 0, current.toolCallsTotal || 0),
          },
        },
      }));
      return response.data;
    } catch (error) {
      if (!owner.ownsRequest(request) || !owner.ownsSelection(sessionID, "tool", selection)) return null;
      if (error instanceof ApiError && error.code.startsWith("invalid_cursor")) {
        owner.notice("Tool history changed. Showing the latest calls; your selected call is unchanged.");
        return this.list(sessionID, { replace: true });
      }
      throw error;
    }
  }

  async select(sessionID, callID) {
    const state = store.getState();
    if (state.ui.selectedSessionID !== sessionID || state.ui.inspector?.mode !== "tool") return null;
    store.setState((state) => {
      const data = state.sessionData[sessionID] || {};
      return {
        ...state,
        ui: { ...state.ui, inspector: { ...state.ui.inspector, callID } },
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...data,
            inspectorSelections: { ...data.inspectorSelections, tool: { callID } },
            toolDetail: data.toolDetail?.id === callID ? data.toolDetail : null,
            toolDetailError: null,
          },
        },
      };
    });
    return this.load(sessionID, callID);
  }

  async load(sessionID, callID) {
    const owner = this.owner;
    const requestOwner = owner.nextRequest(sessionID, "tool:detail");
    const selection = owner.selectedRequest(sessionID, "tool");
    const requestKey = `${sessionID}\u0000${callID}`;
    const owns = () => {
      if (!owner.ownsRequest(requestOwner) || !owner.ownsSelection(sessionID, "tool", selection)) return false;
      const { ui } = store.getState();
      return ui.selectedSessionID !== sessionID || ui.inspector?.mode !== "tool"
        || !ui.inspector.callID || ui.inspector.callID === callID;
    };
    let request = this.detailRequests.get(requestKey);
    if (!request) {
      request = loadToolDetail(sessionID, callID, () => owner.lifecycleEpoch() === requestOwner.lifecycleEpoch);
      this.detailRequests.set(requestKey, request);
      void request.finally(() => {
        if (this.detailRequests.get(requestKey) === request) this.detailRequests.delete(requestKey);
      }).catch(() => {});
    }
    try {
      const { detail, output } = await request;
      if (!owns()) return null;
      owner.setSessionField(sessionID, "toolDetailError", null);
      owner.setSessionField(sessionID, "toolDetail", {
        ...detail.data,
        sequence: detail.data.sequence ?? store.getState().sessionData[sessionID]?.toolCalls?.find((call) => call.id === callID)?.sequence ?? null,
        outputChunks: output.data,
        outputCursor: output.cursor,
      });
      return detail.data;
    } catch (error) {
      if (!owns()) return null;
      owner.setSessionField(sessionID, "toolDetailError", error?.message || String(error));
      throw error;
    }
  }
}

/** Incoming rows already have canonical order. Never fabricate order from IDs. */
export function mergeLatestToolWindow(existing = [], incoming = [], replace = false) {
  existing = existing || [];
  incoming = incoming || [];
  if (replace || !existing.length || !incoming.length) {
    return { calls: incoming, keptOlder: false, replacedOlder: Boolean(existing.length && !incoming.length) };
  }
  const incomingIDs = new Set(incoming.map((call) => call.id));
  const previousByID = new Map(existing.map((call) => [call.id, call]));
  const reordered = incoming.some((call) => {
    const previous = previousByID.get(call.id);
    return previous && Number.isFinite(previous.sequence) && Number.isFinite(call.sequence) && previous.sequence !== call.sequence;
  });
  if (reordered) return { calls: incoming, keptOlder: false, replacedOlder: true };
  const overlapIndex = existing.findIndex((call) => incomingIDs.has(call.id));
  if (overlapIndex < 0) return { calls: incoming, keptOlder: false, replacedOlder: true };
  const older = existing.slice(0, overlapIndex).filter((call) => !incomingIDs.has(call.id));
  if (older.length && Number.isFinite(older.at(-1).sequence) && Number.isFinite(incoming[0]?.sequence)
    && older.at(-1).sequence + 1 !== incoming[0].sequence) {
    return { calls: incoming, keptOlder: false, replacedOlder: true };
  }
  return { calls: [...older, ...incoming], keptOlder: older.length > 0, replacedOlder: false };
}

async function loadToolDetail(sessionID, callID, ownsLifecycle) {
  const known = store.getState().sessionData[sessionID]?.toolCalls?.find((call) => call.id === callID);
  if (known?.retention === "runtime") {
    const [detail, output] = await Promise.all([api.toolCall(sessionID, callID), api.toolOutput(sessionID, callID, 0, 500)]);
    return { detail, output };
  }
  const detail = await api.toolCall(sessionID, callID);
  if (!ownsLifecycle() || detail.data?.retention === "session") {
    return { detail, output: { data: [], cursor: { next: 0, truncatedBefore: 0 } } };
  }
  return { detail, output: await api.toolOutput(sessionID, callID, 0, 500) };
}
