// @ts-check

import { ApiError, api } from "../../api/client.js";
import { store } from "../../state/store.js";
import { beginInspectorSelection } from "./navigation.js";
import { ProcessInspectorState } from "./processes.js";
import { ToolActivityState } from "./tool-activity.js";
import { revealTreeHead } from "./tree-window.js";

const TREE_PAGE_SIZE = 80;

export class InspectorStateController {
  constructor({ lifecycleEpoch, refreshMessages, notice }) {
    this.lifecycleEpoch = lifecycleEpoch;
    this.refreshMessages = refreshMessages;
    this.notice = notice;
    this.selectionVersion = 0;
    this.requestGenerations = new Map();
    this.toolActivity = new ToolActivityState(this);
    this.processes = new ProcessInspectorState(this);
    this.treeMutationChains = new Map();
    this.treeMutationGeneration = new Map();
    this.treePendingLabels = new Map();
    this.treeServerRevisions = new Map();
    this.treeInstalledEpochs = new Map();
  }

  invalidateLifecycle() {
    this.discardPendingTreeLabels();
    this.invalidateSelection();
    this.requestGenerations.clear();
    this.toolActivity.invalidate();
    this.processes.invalidate();
    this.treeMutationChains.clear();
    this.treeMutationGeneration.clear();
    this.treePendingLabels.clear();
    this.treeServerRevisions.clear();
    this.treeInstalledEpochs.clear();
  }

  discardPendingTreeLabels() {
    if (!this.treePendingLabels.size) return;
    store.setState((state) => {
      const sessionData = { ...state.sessionData };
      let changed = false;
      for (const [sessionID, mutations] of this.treePendingLabels) {
        const current = sessionData[sessionID];
        if (!current?.tree?.entries?.length) continue;
        const labels = new Map(current.tree.entries.map((entry) => [entry.id, entry.label]));
        for (const mutation of [...mutations].reverse()) {
          labels.set(mutation.entryID, mutation.previousLabel);
        }
        sessionData[sessionID] = {
          ...current,
          tree: {
            ...current.tree,
            entries: current.tree.entries.map((entry) => (
              labels.has(entry.id) ? { ...entry, label: labels.get(entry.id) } : entry
            )),
          },
        };
        changed = true;
      }
      return changed ? { ...state, sessionData } : state;
    });
  }

  invalidateSelection() {
    this.selectionVersion += 1;
  }

  beginSelection(mode, payload = {}) {
    return beginInspectorSelection(this, mode, payload);
  }

  close() {
    this.invalidateSelection();
    store.setState((state) => ({ ...state, ui: { ...state.ui, inspector: null } }));
  }

  selectedRequest(sessionID, mode) {
    const state = store.getState();
    if (state.ui.selectedSessionID !== sessionID || state.ui.inspector?.mode !== mode) return null;
    return this.selectionVersion;
  }

  ownsSelection(sessionID, mode, selectionVersion) {
    if (selectionVersion === null) return true;
    const state = store.getState();
    return (
      this.selectionVersion === selectionVersion &&
      state.ui.selectedSessionID === sessionID &&
      state.ui.inspector?.mode === mode
    );
  }

  nextRequest(sessionID, resource) {
    const key = `${sessionID}\u0000${resource}`;
    const generation = (this.requestGenerations.get(key) || 0) + 1;
    this.requestGenerations.set(key, generation);
    return { key, generation, lifecycleEpoch: this.lifecycleEpoch() };
  }

  invalidateRequest(sessionID, resource) {
    const key = `${sessionID}\u0000${resource}`;
    this.requestGenerations.set(key, (this.requestGenerations.get(key) || 0) + 1);
  }

  ownsRequest(request) {
    return (
      this.lifecycleEpoch() === request.lifecycleEpoch &&
      this.requestGenerations.get(request.key) === request.generation
    );
  }

  setSessionField(sessionID, key, value) {
    store.setState((state) => ({
      ...state,
      sessionData: {
        ...state.sessionData,
        [sessionID]: { ...state.sessionData[sessionID], [key]: value },
      },
    }));
  }

  async refreshTree(sessionID) {
    const state = store.getState();
    if (!state.sessionData[sessionID]?.tree && state.ui.inspector?.mode !== "tree") return;
    const request = this.nextRequest(sessionID, "tree:latest");
    const selection = this.selectedRequest(sessionID, "tree");
    const response = await api.tree(sessionID, { limit: TREE_PAGE_SIZE });
    if (!this.ownsRequest(request) || !this.ownsSelection(sessionID, "tree", selection)) return;
    this.installLatestTree(sessionID, response.data);
  }

  installLatestTree(sessionID, incoming) {
    const incomingRevision = treeRevision(incoming);
    const current = store.getState().sessionData[sessionID]?.tree || null;
    const currentRevision = treeRevision(current);
    const currentIsOwned = this.treeInstalledEpochs.get(sessionID) === this.lifecycleEpoch();
    const knownRevision = this.treeServerRevisions.get(sessionID);
    if (
      (knownRevision !== undefined && incomingRevision < knownRevision) ||
      (currentIsOwned && incomingRevision < currentRevision)
    ) return false;

    const pending = this.currentPendingLabels(sessionID);
    let authoritative = incoming;
    if (currentIsOwned && incomingRevision === currentRevision) {
      const latestIDs = new Set((incoming.entries || []).map((entry) => entry.id));
      const older = (current.entries || []).filter((entry) => !latestIDs.has(entry.id));
      authoritative = {
        ...incoming,
        entries: [...older, ...(incoming.entries || [])],
        cursor: current.cursor,
      };
    }
    this.treeServerRevisions.set(sessionID, incomingRevision);
    this.treeInstalledEpochs.set(sessionID, this.lifecycleEpoch());
    this.setSessionField(sessionID, "tree", applyPendingTreeLabels(authoritative, pending));
    return true;
  }

  async loadMoreTree(sessionID) {
    const starting = store.getState().sessionData[sessionID]?.tree;
    const cursor = starting?.cursor?.next;
    if (!cursor) return;
    const lifecycleEpoch = this.lifecycleEpoch();
    const selection = this.selectedRequest(sessionID, "tree");
    const response = await api.tree(sessionID, { limit: TREE_PAGE_SIZE, cursor });
    if (
      this.lifecycleEpoch() !== lifecycleEpoch ||
      !this.ownsSelection(sessionID, "tree", selection)
    ) return;

    const current = store.getState().sessionData[sessionID]?.tree;
    if (!current || current.cursor?.next !== cursor) return;
    const incomingRevision = treeRevision(response.data);
    const currentRevision = treeRevision(current);
    const knownRevision = this.treeServerRevisions.get(sessionID);
    if (
      this.treeInstalledEpochs.get(sessionID) !== lifecycleEpoch ||
      (knownRevision !== undefined && incomingRevision < knownRevision) ||
      incomingRevision < currentRevision
    ) return;
    if (incomingRevision > currentRevision) {
      const lifecycleEpoch = this.lifecycleEpoch();
      void this.refreshTree(sessionID).catch((error) => {
        if (this.lifecycleEpoch() === lifecycleEpoch) this.notice(errorMessage(error));
      });
      return;
    }

    const currentIDs = new Set((current.entries || []).map((entry) => entry.id));
    const older = (response.data.entries || []).filter((entry) => !currentIDs.has(entry.id));
    const pending = this.currentPendingLabels(sessionID);
    this.treeServerRevisions.set(sessionID, incomingRevision);
    this.treeInstalledEpochs.set(sessionID, lifecycleEpoch);
    this.setSessionField(sessionID, "tree", applyPendingTreeLabels({
      ...current,
      ...response.data,
      entries: [...older, ...(current.entries || [])],
      cursor: response.data.cursor,
    }, pending));
  }

  async treePreview(sessionID, targetID) {
    const request = this.nextRequest(sessionID, "tree:preview");
    const selection = this.selectedRequest(sessionID, "tree");
    const response = await api.treePreview(sessionID, targetID);
    if (!this.ownsRequest(request) || !this.ownsSelection(sessionID, "tree", selection)) return null;
    this.setSessionField(sessionID, "treePreview", response.data);
    return response.data;
  }

  clearTreePreview(sessionID) {
    this.invalidateRequest(sessionID, "tree:preview");
    this.setSessionField(sessionID, "treePreview", null);
  }

  async navigateTree(sessionID, targetID, branchSummary = null) {
    const tree = store.getState().sessionData[sessionID]?.tree;
    if (!tree) return;
    const lifecycleEpoch = this.lifecycleEpoch();
    try {
      const response = await api.navigateTree(sessionID, {
        expectedRevision: tree.revision,
        targetID,
        branchSummary,
      });
      if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
      await Promise.all([this.refreshTree(sessionID), this.refreshMessages(sessionID)]);
      if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
      this.setSessionField(sessionID, "treePreview", null);
      if (response.data.editorText) this.setSessionField(sessionID, "editorHandoff", response.data.editorText);
      return response.data;
    } catch (error) {
      if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
      if (error instanceof ApiError && error.code === "tree_revision_conflict") {
        await this.refreshTree(sessionID);
        if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
        this.notice("Session tree changed elsewhere. Refreshed before navigation.");
        return null;
      }
      throw error;
    }
  }

  async labelTreeEntry(sessionID, entryID, label) {
    const tree = store.getState().sessionData[sessionID]?.tree;
    if (!tree) return;
    const normalized = String(label || "").trim().replace(/\s+/g, " ") || null;
    const previousLabel = tree.entries?.find((entry) => entry.id === entryID)?.label ?? null;
    const generation = (this.treeMutationGeneration.get(sessionID) || 0) + 1;
    this.treeMutationGeneration.set(sessionID, generation);
    const lifecycleEpoch = this.lifecycleEpoch();
    const mutation = { generation, entryID, label: normalized, previousLabel, lifecycleEpoch };
    const pending = [...(this.treePendingLabels.get(sessionID) || []), mutation];
    this.treePendingLabels.set(sessionID, pending);
    if (!this.treeServerRevisions.has(sessionID)) {
      this.treeServerRevisions.set(sessionID, treeRevision(tree));
    }
    this.setSessionField(sessionID, "tree", applyPendingTreeLabels(tree, pending));

    const prior = this.treeMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
      const expectedRevision = this.treeServerRevisions.get(sessionID) || 0;
      try {
        const response = await api.patchTreeEntry(sessionID, entryID, {
          expectedRevision,
          label: normalized,
        });
        if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
        this.treeServerRevisions.set(sessionID, treeRevision(response.data));
        this.treeInstalledEpochs.set(sessionID, this.lifecycleEpoch());
        const remaining = this.removePendingLabel(sessionID, generation);
        const current = store.getState().sessionData[sessionID]?.tree;
        if (current) {
          const authoritative = mergeTreeEntry(current, response.data.entry, response.data.revision);
          this.setSessionField(sessionID, "tree", applyPendingTreeLabels(authoritative, remaining));
        }
        return response.data;
      } catch (error) {
        if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
        const remaining = this.removePendingLabel(sessionID, generation, lifecycleEpoch);
        try {
          const refreshed = await api.tree(sessionID);
          if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
          this.installLatestTree(sessionID, applyPendingTreeLabels(refreshed.data, remaining));
        } catch {
          // SSE or the next tree refresh will reconcile if recovery also fails.
        }
        if (this.lifecycleEpoch() !== lifecycleEpoch) return null;
        if (error instanceof ApiError && error.code === "tree_revision_conflict") {
          this.notice("Session tree changed elsewhere. Refreshed before labeling.");
          return null;
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.treeMutationChains.get(sessionID) === chained) this.treeMutationChains.delete(sessionID);
    });
    this.treeMutationChains.set(sessionID, chained);
    return chained;
  }

  currentPendingLabels(sessionID) {
    const lifecycleEpoch = this.lifecycleEpoch();
    return (this.treePendingLabels.get(sessionID) || [])
      .filter((item) => item.lifecycleEpoch === lifecycleEpoch);
  }

  removePendingLabel(sessionID, generation, lifecycleEpoch = this.lifecycleEpoch()) {
    const remaining = (this.treePendingLabels.get(sessionID) || [])
      .filter((item) => (
        item.lifecycleEpoch === lifecycleEpoch && item.generation !== generation
      ));
    if (remaining.length) this.treePendingLabels.set(sessionID, remaining);
    else this.treePendingLabels.delete(sessionID);
    return remaining;
  }

  async listToolCalls(sessionID) {
    return this.toolActivity.list(sessionID);
  }

  async loadToolCall(sessionID, callID) {
    return this.toolActivity.load(sessionID, callID);
  }

  async selectToolCall(sessionID, callID) {
    return this.toolActivity.select(sessionID, callID);
  }

  async loadProcess(processID) {
    return this.processes.load(processID);
  }

  async refreshProcessOutput(processID) {
    return this.processes.refreshOutput(processID);
  }

  findProcessDetail(processID) {
    return this.processes.find(processID);
  }

  async refreshProcess(processID) {
    return this.processes.refresh(processID);
  }

  loadMoreToolCalls(sessionID) { return this.toolActivity.loadOlder(sessionID); }

  showLatestToolCalls(sessionID) { return this.toolActivity.list(sessionID, { replace: true }); }

  revealTreeHead(sessionID) {
    return revealTreeHead(this, sessionID);
  }
}

function treeRevision(tree) {
  const revision = Number(tree?.revision || 0);
  return Number.isFinite(revision) ? revision : 0;
}

function applyPendingTreeLabels(tree, pending) {
  if (!tree?.entries?.length || !pending?.length) return tree;
  const labels = new Map();
  for (const mutation of pending) labels.set(mutation.entryID, mutation.label);
  return {
    ...tree,
    entries: tree.entries.map((entry) => labels.has(entry.id)
      ? { ...entry, label: labels.get(entry.id) }
      : entry),
  };
}

function mergeTreeEntry(tree, entry, revision) {
  if (!tree?.entries?.length) return tree;
  return {
    ...tree,
    revision,
    entries: tree.entries.map((item) => item.id === entry.id ? entry : item),
  };
}

function errorMessage(error) {
  if (error instanceof ApiError) {
    return `${error.message}${error.requestID ? ` (${error.requestID})` : ""}`;
  }
  return error?.message || String(error);
}
