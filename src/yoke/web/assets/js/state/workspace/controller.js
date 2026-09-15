import { api } from "../../api/client.js";
import { store } from "../store.js";
import { mergeSessionSummary } from "../reducer.js";
import { requireAvailableWorkspace } from "./status.js";

export class WorkspaceController {
  constructor(host) {
    this.host = host;
    this.epoch = 0;
    this.pendingRelocations = new Set();
    api.captureError = () => {
      const owns = this.guard();
      return (error) => { if (owns()) this.handleError(error); };
    };
  }

  guard() {
    const epoch = this.epoch;
    const lifecycle = this.host.lifecycleEpoch;
    return () => epoch === this.epoch && this.host.ownsLifecycle(lifecycle);
  }

  require(sessionID) {
    const state = store.getState();
    requireAvailableWorkspace(state.sessions[sessionID], state.active[sessionID]);
  }

  reconcileMetadata(sessionID, summary, owns) {
    if (owns()) {
      this.observe(sessionID, summary);
      return summary;
    }
    // Rename/pin still settle, but their old workspace snapshot no longer owns
    // the binding. The same rule applies to failed-mutation rollback snapshots.
    const current = store.getState().sessions[sessionID];
    this.host.schedule(`summary:${sessionID}`, 0, () => this.host.refreshSessionSummary(sessionID));
    return { ...summary, location: current?.location, workspace: current?.workspace };
  }

  relocated(event, delay) {
    const sessionID = event.sessionID;
    const directory = store.getState().sessions[sessionID]?.location?.directory;
    this.pendingRelocations.add(sessionID);
    this.invalidate(sessionID, [directory, event.data?.previousDirectory, event.data?.directory].filter(Boolean));
    this.host.schedule(`workspace:${sessionID}`, delay, async () => {
      const lifecycle = this.host.lifecycleEpoch;
      // Observing the summary advances the generation again. Start the list
      // request afterwards so that it belongs to the new binding.
      await this.host.refreshSessionSummary(sessionID);
      if (!this.host.ownsLifecycle(lifecycle)) return;
      await this.host.refreshSessionLists();
    });
  }

  handleError(error) {
    if (error?.code !== "session_workspace_unavailable") return;
    const { sessionID, directory, status } = error.details || {};
    const session = store.getState().sessions[sessionID];
    if (!session || (directory && session.location?.directory !== directory)) return;
    if (!session.workspace || session.workspace.status === "available") {
      this.invalidate(sessionID, [session.location?.directory].filter(Boolean));
    }
    store.setState((state) => mergeSessionSummary(state, {
      ...state.sessions[sessionID],
      workspace: { status: status || "missing", message: error.message },
    }));
  }

  observe(sessionID, session) {
    if (session.workspace?.status === "available") {
      store.setState((state) => {
        const runtime = state.active[sessionID];
        if (runtime?.lastError?.code !== "session_workspace_unavailable") return state;
        return { ...state, active: { ...state.active, [sessionID]: { ...runtime, lastError: null } } };
      });
    }
    const previous = store.getState().sessions[sessionID];
    const oldDirectory = previous?.location?.directory;
    const directory = session.location?.directory;
    const relocated = this.pendingRelocations.delete(sessionID);
    if (!relocated && (!previous || oldDirectory === directory)) return;
    this.invalidate(sessionID, [oldDirectory, directory].filter(Boolean));
    // A different browser or CLI may have relocated the session.
    store.setState((state) => mergeSessionSummary(state, session));
    void Promise.allSettled([
      this.refreshTools(sessionID), this.refreshSkills(sessionID), this.refreshMcp(sessionID),
      this.loadProviders(directory), this.loadModels(directory, session.selection?.provider),
      this.refreshLocations(directory),
    ]);
  }

  invalidate(sessionID, directories) {
    this.epoch += 1;
    for (const key of ["config:tools", "config:skills", "config:mcp", "file:detail"]) {
      this.host.inspectorState.invalidateRequest(sessionID, key);
    }
    store.setState((state) => {
      const data = state.sessionData[sessionID] || {};
      const models = Object.fromEntries(Object.entries(state.models || {}).filter(
        ([key]) => !directories.some((directory) => key.startsWith(`${directory}:`)),
      ));
      const providerCatalogs = { ...state.providerCatalogs };
      const locations = { ...state.locations };
      for (const directory of directories) {
        delete providerCatalogs[directory];
        delete locations[directory];
      }
      return {
        ...state, models, providerCatalogs, locations,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...data, tools: null, skills: null, mcp: null, fileDetail: null,
            inspectorPending: {}, inspectorErrors: {},
          },
        },
      };
    });
  }

  async relocate(sessionID, directory, expectedDirectory) {
    const owns = this.guard();
    const lifecycle = this.host.lifecycleEpoch;
    const response = await api.relocateSession(sessionID, directory, expectedDirectory);
    if (!owns()) {
      if (!this.host.ownsLifecycle(lifecycle)) return null;
      // Our own SSE event can precede the POST response. Read the current
      // binding instead of installing a response from the retired generation.
      await this.host.refreshSessionSummary(sessionID);
      if (!this.host.ownsLifecycle(lifecycle)) return null;
      const current = store.getState().sessions[sessionID];
      return current?.location?.directory === response.data.location.directory ? current : null;
    }
    const oldDirectory = store.getState().sessions[sessionID]?.location?.directory;
    const session = response.data;
    this.invalidate(sessionID, [oldDirectory, session.location.directory].filter(Boolean));
    store.setState((state) => {
      const active = { ...state.active };
      if (active[sessionID]?.lastError?.code === "session_workspace_unavailable") delete active[sessionID];
      return { ...mergeSessionSummary(state, session), active };
    });
    // Relocation has succeeded even if one of these catalogs cannot be read.
    const refreshOwns = this.guard();
    const results = await Promise.allSettled([
      this.refreshTools(sessionID), this.refreshSkills(sessionID), this.refreshMcp(sessionID),
      this.loadProviders(session.location.directory),
      this.loadModels(session.location.directory, session.selection?.provider),
      this.refreshLocations(session.location.directory),
      this.host.resolveVisibleLocations(), this.host.refreshSessionLists(),
    ]);
    if (refreshOwns()) {
      const failures = results.filter((result) => result.status === "rejected");
      this.host.notice(failures.length
        ? "Workspace relocated. Some catalogs could not refresh. Reopen the inspector or model picker to retry."
        : "Workspace relocated. Session history and draft kept.");
    }
    return session;
  }

  async refreshLocations(directory) {
    const owns = this.guard();
    const results = await Promise.allSettled([
      api.resolveLocation(directory).then((response) => {
        if (owns()) store.setState((state) => ({
          ...state, locations: { ...state.locations, [directory]: response.data },
        }));
      }),
      api.recentLocations().then((response) => {
        if (owns()) store.setState((state) => ({ ...state, recentLocations: response.data }));
      }),
    ]);
    const failure = results.find((result) => result.status === "rejected");
    if (failure) throw failure.reason;
  }

  async catalog(sessionID, mode, field, load) {
    const owns = this.guard();
    const request = this.host.inspectorState.nextRequest(sessionID, `config:${mode}`);
    const selection = this.host.inspectorState.selectedRequest(sessionID, mode);
    const response = await load();
    if (!owns() || !this.host.inspectorState.ownsRequest(request)
      || !this.host.inspectorState.ownsSelection(sessionID, mode, selection)) return;
    this.host.setSessionField(sessionID, field, response.data);
  }

  refreshTools(sessionID) {
    const session = store.getState().sessions[sessionID];
    if (!session) return;
    return this.catalog(sessionID, "tools", "tools", () => api.tools({ directory: session.location.directory, sessionID }));
  }

  refreshSkills(sessionID) {
    return this.catalog(sessionID, "skills", "skills", () => api.sessionSkills(sessionID));
  }

  refreshMcp(sessionID) {
    return this.catalog(sessionID, "mcp", "mcp", () => api.sessionMcp(sessionID, true));
  }

  async loadModels(directory, provider = null, search = null) {
    const owns = this.guard();
    const response = await api.models({ directory, provider, search });
    if (!owns()) return [];
    store.setState((state) => ({
      ...state,
      models: { ...state.models, [`${directory || ""}:${provider || ""}:${search || ""}`]: response.data },
    }));
    return response.data;
  }

  async loadProviders(directory = null) {
    const owns = this.guard();
    const response = await api.providers(directory);
    if (!owns()) return [];
    store.setState((state) => ({
      ...state,
      providerCatalogs: { ...state.providerCatalogs, [directory || ""]: response.data },
    }));
    return response.data;
  }
}
