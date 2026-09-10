import { api } from "../../api/client.js";
import { store } from "../../state/store.js";

const CONFIGURATION_MODES = new Set(["tools", "skills", "mcp"]);

export function beginInspectorSelection(owner, mode, payload = {}) {
  const sessionID = store.getState().ui.selectedSessionID;
  if (!sessionID) return null;
  owner.invalidateSelection();
  const selectionVersion = owner.selectionVersion;
  let inspector;
  store.setState((state) => {
    const data = state.sessionData[sessionID] || {};
    const saved = data.inspectorSelections?.[mode] || {};
    const previous = state.ui.inspector;
    const from = payload.from
      ? { ...payload.from, from: payload.from.from || (previous?.mode === payload.from.mode ? previous.from : null) }
      : null;
    inspector = { ...saved, ...payload, mode, from };
    const resolveRuntime = mode === "process" && payload.runtimeSessionID != null && !payload.processID;
    if (resolveRuntime) inspector.processID = null;
    if (mode === "tool" && !inspector.callID && data.toolDetail?.id) inspector.callID = data.toolDetail.id;
    const remembered = mode === "tool" ? { callID: inspector.callID || null }
      : mode === "process" ? { processID: inspector.processID || data.processDetail?.processID || null }
        : {};
    const nextData = {
      ...data,
      inspectorSelections: { ...data.inspectorSelections, [mode]: remembered },
      inspectorErrors: { ...data.inspectorErrors, [mode]: null },
    };
    if (CONFIGURATION_MODES.has(mode)) nextData.inspectorConfigurationMode = mode;
    if (mode === "tool" && inspector.callID && data.toolDetail?.id !== inspector.callID) {
      nextData.toolDetail = null;
      nextData.toolDetailError = null;
    }
    if (resolveRuntime || (mode === "process" && inspector.processID && data.processDetail?.processID !== inspector.processID)) {
      nextData.processDetail = null;
      nextData.processDetailError = null;
    }
    if (mode === "file" && data.fileDetail?.path !== inspector.path) nextData.fileDetail = null;
    return {
      ...state,
      ui: { ...state.ui, inspector },
      sessionData: { ...state.sessionData, [sessionID]: nextData },
    };
  });
  return { sessionID, mode, selectionVersion, inspector };
}

export async function openInspector(host, mode, payload = {}) {
  host.clearNotice();
  const selection = host.inspectorState.beginSelection(mode, payload);
  if (!selection) return null;
  const { sessionID, selectionVersion, inspector } = selection;
  const request = host.inspectorState.nextRequest(sessionID, `view:${mode}`);
  const owns = () => host.inspectorState.ownsRequest(request)
    && host.inspectorState.ownsSelection(sessionID, mode, selectionVersion);
  updateViewStatus(sessionID, mode, { pending: true, error: null });
  try {
    if (mode === "tree") await host.refreshTree(sessionID);
    if (mode === "process") {
      if (inspector.runtimeSessionID != null && !payload.processID) {
        const resolution = host.inspectorState.nextRequest(sessionID, "process:selection");
        await host.refreshProcesses(sessionID);
        if (!owns() || !host.inspectorState.ownsRequest(resolution)) return null;
        const match = store.getState().sessionData[sessionID]?.processes?.find((process) => (
          String(process.runtimeSessionID) === String(inspector.runtimeSessionID)
        ));
        if (!match) throw new Error(`Process ${inspector.runtimeSessionID} is no longer retained. The originating tool result is still available.`);
        store.setState((state) => ({ ...state, ui: { ...state.ui, inspector: { ...state.ui.inspector, processID: match.processID } } }));
        await host.loadProcess(match.processID);
      } else {
        await Promise.all([
          host.refreshProcesses(sessionID),
          inspector.processID ? host.loadProcess(inspector.processID) : Promise.resolve(),
        ]);
      }
    }
    if (mode === "tool") {
      await Promise.all([
        host.listToolCalls(sessionID),
        inspector.callID ? host.loadToolCall(sessionID, inspector.callID) : Promise.resolve(),
      ]);
      if (owns() && !store.getState().ui.inspector?.callID) {
        const latest = store.getState().sessionData[sessionID]?.toolCalls?.at(-1);
        if (latest) await host.selectToolCall(sessionID, latest.id);
      }
    }
    if (mode === "tools") await host.refreshTools(sessionID);
    if (mode === "skills") await host.refreshSkills(sessionID);
    if (mode === "mcp") await host.refreshMcp(sessionID);
    if (mode === "context") {
      const response = await api.context(sessionID);
      if (owns()) host.setSessionField(sessionID, "context", response.data);
    }
    if (mode === "file" && inspector.path) {
      await loadInspectorFile(host, sessionID, inspector.path, selectionVersion);
    }
    return owns() ? selection : null;
  } catch (error) {
    if (owns()) updateViewStatus(sessionID, mode, { error: error?.message || String(error) });
    return null;
  } finally {
    if (owns()) updateViewStatus(sessionID, mode, { pending: false });
  }
}

export function backInspector(host) {
  const inspector = store.getState().ui.inspector;
  if (!inspector) return;
  if (inspector.from?.mode) {
    const { mode, ...payload } = inspector.from;
    return host.openInspector(mode, payload);
  }
  if (inspector.mode === "file") return host.openInspector("tool");
  host.closeInspector();
}

export async function loadInspectorFile(host, sessionID, path, selectionVersion = null) {
  const session = store.getState().sessions[sessionID];
  if (!session) return;
  const request = host.inspectorState.nextRequest(sessionID, "file:detail");
  const content = await api.fsRead(session.location.directory, path);
  if (!host.inspectorState.ownsRequest(request)) return;
  if (!host.inspectorState.ownsSelection(sessionID, "file", selectionVersion)) return;
  host.setSessionField(sessionID, "fileDetail", { path, content });
}

function updateViewStatus(sessionID, mode, { pending, error }) {
  store.setState((state) => {
    const data = state.sessionData[sessionID] || {};
    return {
      ...state,
      sessionData: {
        ...state.sessionData,
        [sessionID]: {
          ...data,
          ...(pending !== undefined ? { inspectorPending: { ...data.inspectorPending, [mode]: pending } } : {}),
          ...(error !== undefined ? { inspectorErrors: { ...data.inspectorErrors, [mode]: error } } : {}),
        },
      },
    };
  });
}
