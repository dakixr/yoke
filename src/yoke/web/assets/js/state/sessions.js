// @ts-check

import { api } from "../api/client.js";
import { mergeSessionSummary } from "./reducer.js";
import { installSessionSummary, optimisticSessionPatch, restoreSessionSummary } from "./optimistic-projections.js";
import { store } from "./store.js";

/**
 * Owns session lists/search and serialized metadata mutations.
 * Workspace guards fence location snapshots; input/configuration owners project theirs.
 */
export class SessionController {
  constructor({ lifecycle, workspace, actions, inputs, configuration }) {
    this.lifecycle = lifecycle;
    this.workspace = workspace;
    this.actions = actions;
    this.inputs = inputs;
    this.configuration = configuration;
    this.searchGeneration = 0;
    this.optimisticSessionGeneration = new Map();
    this.sessionMutationChains = new Map();
    this.sessionPendingMutations = new Map();
  }

  retire() {
    this.optimisticSessionGeneration.clear();
    this.sessionMutationChains.clear();
    this.sessionPendingMutations.clear();
  }

  async refreshSessionLists() {
    const ownsWorkspace = this.workspace.guard();
    const lifecycleEpoch = this.lifecycle.epoch;
    const [current, archived] = await Promise.all([
      api.listSessions({ archived: false, limit: 100, order: "lastUserDesc" }),
      api.listSessions({ archived: true, limit: 30, order: "lastUserDesc" }),
    ]);
    if (this.lifecycle.epoch !== lifecycleEpoch || !ownsWorkspace()) return;
    for (const session of [...current.data, ...archived.data]) this.workspace.observe(session.id, session);
    store.setState((state) => {
      let next = this.inputs.installLists(state, current, archived);
      for (const [sessionID, mutations] of this.sessionPendingMutations) {
        if (!mutations.length) continue;
        const base = next.sessions[sessionID] || state.sessions[sessionID];
        if (!base) continue;
        let visible = base;
        for (const mutation of mutations) visible = optimisticSessionPatch(visible, mutation.patch);
        next = installSessionSummary(next, visible);
      }
      return this.configuration.projectPendingSelections(next);
    });
    void this.actions.resolveVisibleLocations();
  }

  async loadMoreSessions(archived = false) {
    const ownsWorkspace = this.workspace.guard();
    const state = store.getState();
    const cursor = archived ? state.archivedCursor : state.sessionsCursor;
    if (!cursor) return;
    const lifecycleEpoch = this.lifecycle.epoch;
    const response = await api.listSessions({
      archived,
      limit: archived ? 30 : 100,
      order: "lastUserDesc",
      cursor,
    });
    if (!this.lifecycle.owns(lifecycleEpoch) || !ownsWorkspace()) return;
    for (const session of response.data) this.workspace.observe(session.id, session);
    store.setState((current) => {
      const sessions = { ...current.sessions };
      for (const item of response.data) {
        sessions[item.id] = this.inputs.mergeSummary(
          sessions[item.id],
          item,
        );
      }
      const key = archived ? "archivedOrder" : "sessionOrder";
      return {
        ...current,
        sessions,
        [key]: [...current[key], ...response.data.map((item) => item.id)],
        ...(archived && Number.isFinite(response.total) ? { archivedTotal: response.total } : {}),
        [archived ? "archivedCursor" : "sessionsCursor"]: response.cursor?.next || null,
      };
    });
    void this.actions.resolveVisibleLocations();
  }

  async countSessions({ directory = null, archived = false } = {}) {
    const response = await api.listSessions({
      directory: directory || undefined,
      archived,
      limit: 1,
      order: "lastUserDesc",
    });
    return Number.isFinite(response.total) ? response.total : response.data.length;
  }

  async refreshSessionSummary(sessionID) {
    const ownsWorkspace = this.workspace.guard();
    const lifecycleEpoch = this.lifecycle.epoch;
    const response = await api.getSession(sessionID);
    if (this.lifecycle.epoch !== lifecycleEpoch || !ownsWorkspace()) return;
    this.workspace.observe(sessionID, response.data);
    store.setState((state) => {
      let summary = response.data;
      const localLastUserMessage = state.sessions[sessionID]?.time?.lastUserMessage || null;
      const serverLastUserMessage = summary.time?.lastUserMessage || null;
      const localLastUserTime = Date.parse(localLastUserMessage || "");
      const serverLastUserTime = Date.parse(serverLastUserMessage || "");
      if (
        localLastUserMessage && Number.isFinite(localLastUserTime) &&
        (!Number.isFinite(serverLastUserTime) || localLastUserTime > serverLastUserTime)
      ) {
        summary = {
          ...summary,
          time: { ...summary.time, lastUserMessage: localLastUserMessage },
        };
      }
      for (const mutation of this.sessionPendingMutations.get(sessionID) || []) {
        summary = optimisticSessionPatch(summary, mutation.patch);
      }
      summary = this.configuration.projectSelection(summary);
      summary = this.inputs.mergeSummary(
        state.sessions[sessionID],
        summary,
      );
      return mergeSessionSummary(state, summary);
    });
  }

  async resolveVisibleLocations() {
    const ownsWorkspace = this.workspace.guard();
    const lifecycleEpoch = this.lifecycle.epoch;
    const state = store.getState();
    const directories = [...new Set(
      [...state.sessionOrder, ...state.archivedOrder]
        .map((id) => state.sessions[id]?.location?.directory)
        .filter(Boolean),
    )].filter((directory) => !state.locations[directory]);
    let next = 0;
    const worker = async () => {
      while (next < directories.length) {
        if (!this.lifecycle.owns(lifecycleEpoch)) return;
        const directory = directories[next++];
        if (store.getState().locations[directory]) continue;
        try {
          const response = await api.resolveLocation(directory);
          if (!this.lifecycle.owns(lifecycleEpoch) || !ownsWorkspace()) return;
          store.setState((current) => ({
            ...current,
            locations: { ...current.locations, [directory]: response.data },
          }));
        } catch {
          if (!this.lifecycle.owns(lifecycleEpoch) || !ownsWorkspace()) return;
          store.setState((current) => ({
            ...current,
            locations: {
              ...current.locations,
              [directory]: { directory, name: directory.split("/").filter(Boolean).at(-1) || directory, git: null },
            },
          }));
        }
      }
    };
    await Promise.all(
      Array.from({ length: Math.min(8, directories.length) }, () => worker()),
    );
  }

  async searchSessions(query) {
    const generation = ++this.searchGeneration;
    const ownsWorkspace = this.workspace.guard();
    const lifecycleEpoch = this.lifecycle.epoch;
    const value = query.trim();
    store.setState((state) => ({ ...state, ui: { ...state.ui, search: query, searching: Boolean(value) } }));
    if (!value) {
      store.setState((state) => ({ ...state, ui: { ...state.ui, searchResults: [], searching: false } }));
      return;
    }
    let response;
    try {
      response = await api.listSessions({ search: value, limit: 100 });
    } catch (error) {
      if (
        this.lifecycle.owns(lifecycleEpoch)
        && generation === this.searchGeneration
        && store.getState().ui.search === query
      ) {
        store.setState((state) => ({ ...state, ui: { ...state.ui, searching: false } }));
      }
      throw error;
    }
    if (!this.lifecycle.owns(lifecycleEpoch) || generation !== this.searchGeneration) return;
    if (!ownsWorkspace()) {
      // A relocation or typed workspace failure retired the binding snapshot
      // used by this request. Retry the still-current query instead of leaving
      // the sidebar in a permanent loading state.
      if (store.getState().ui.search === query) {
        void this.searchSessions(query).catch(() => {});
      }
      return;
    }
    for (const session of response.data) this.workspace.observe(session.id, session);
    if (!this.lifecycle.owns(lifecycleEpoch) || generation !== this.searchGeneration) return;
    if (!ownsWorkspace()) {
      if (store.getState().ui.search === query) {
        void this.searchSessions(query).catch(() => {});
      }
      return;
    }
    store.setState((state) => {
      const sessions = { ...state.sessions };
      for (const item of response.data) {
        sessions[item.id] = this.inputs.mergeSummary(
          sessions[item.id],
          item,
        );
      }
      return {
        ...state,
        sessions,
        ui: { ...state.ui, searchResults: response.data.map((item) => item.id), searching: false },
      };
    });
    void this.actions.resolveVisibleLocations();
  }

  async patchSession(sessionID, patch) {
    const before = store.getState();
    const previous = before.sessions[sessionID] || null;
    const activeIndex = before.sessionOrder.indexOf(sessionID);
    const archivedIndex = before.archivedOrder.indexOf(sessionID);
    const generation = (this.optimisticSessionGeneration.get(sessionID) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    const ownsPreviousWorkspace = this.workspace.guard();
    this.optimisticSessionGeneration.set(sessionID, generation);
    const mutation = { generation, patch, previous, activeIndex, archivedIndex, lifecycleEpoch };
    this.sessionPendingMutations.set(
      sessionID,
      [...(this.sessionPendingMutations.get(sessionID) || []), mutation],
    );
    if (previous) {
      store.setState((state) => installSessionSummary(state, optimisticSessionPatch(previous, patch)));
    }

    const prior = this.sessionMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      const ownsWorkspace = this.workspace.guard();
      const removePending = () => {
        const remaining = (this.sessionPendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        if (remaining.length) this.sessionPendingMutations.set(sessionID, remaining);
        else this.sessionPendingMutations.delete(sessionID);
        return remaining;
      };
      const reconcile = (summary, owns, remaining) => {
        let visible = this.workspace.reconcileMetadata(sessionID, summary, owns);
        for (const item of remaining) visible = optimisticSessionPatch(visible, item.patch);
        return this.configuration.projectSelection(visible);
      };
      try {
        const response = await api.patchSession(sessionID, patch);
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        const visible = reconcile(response.data, ownsWorkspace, removePending());
        store.setState((state) => installSessionSummary(state, visible));
        return response.data;
      } catch (error) {
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        const remaining = removePending();
        try {
          const ownsRefreshWorkspace = this.workspace.guard();
          const response = await api.getSession(sessionID);
          if (!this.lifecycle.owns(lifecycleEpoch)) return null;
          const visible = reconcile(response.data, ownsRefreshWorkspace, remaining);
          store.setState((state) => installSessionSummary(state, visible));
        } catch {
          if (!this.lifecycle.owns(lifecycleEpoch)) return null;
          if (previous) {
            const visible = reconcile(previous, ownsPreviousWorkspace, remaining);
            store.setState((state) => remaining.length
              ? installSessionSummary(state, visible)
              : restoreSessionSummary(state, visible, activeIndex, archivedIndex));
          }
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.sessionMutationChains.get(sessionID) === chained) {
        this.sessionMutationChains.delete(sessionID);
      }
    });
    this.sessionMutationChains.set(sessionID, chained);
    return chained;
  }

  async regenerateTitle(sessionID) {
    this.workspace.require(sessionID);
    const lifecycleEpoch = this.lifecycle.epoch;
    const response = await api.regenerateTitle(sessionID);
    if (!this.lifecycle.owns(lifecycleEpoch)) return null;
    store.setState((state) => mergeSessionSummary(
      state,
      this.inputs.mergeSummary(
        state.sessions[sessionID],
        response.data,
      ),
    ));
    await this.actions.refreshSessionLists();
    if (!this.lifecycle.owns(lifecycleEpoch)) return null;
    return response.data;
  }

  async forkSession(sessionID, location = null) {
    const lifecycleEpoch = this.lifecycle.epoch;
    this.actions.pendingNotice("Forking session…");
    try {
      const response = await api.forkSession(sessionID, location ? { location } : {});
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      store.setState((state) => installSessionSummary(state, response.data, { moveToFront: true }));
      this.actions.selectSession(response.data.id);
      await this.actions.loadSession(response.data.id, { force: true });
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      void this.actions.refreshSessionLists().catch(() => {});
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      this.actions.clearNotice();
      throw error;
    }
  }
}
