// @ts-check

import { errorMessage } from "../lib/errors.js";
import { ApiError, api } from "../api/client.js";
import { randomUUID } from "../lib/id.js";
import { admitSubmission, optimisticRuntime, rollbackRuntime, isBusy } from "./session-machine.js";
import { installInputs, installSessionMachine } from "./reducer.js";
import {
  installSessionLists,
  mergeServerSessionSummary,
  applyQueueOperations,
  installSessionSummary,
  queueSummary,
  projectSubmission,
  rollbackSubmission,
  restoreSessionSummary,
} from "./optimistic-projections.js";
import { store } from "./store.js";

const SUMMARY_REFRESH_MS = 250;

/**
 * Owns prompt admission and queue mutations, revisions and pending operations.
 * Summary projection preserves the authoritative queue revision for other owners.
 */
export class InputController {
  constructor({ lifecycle, workspace, actions }) {
    this.lifecycle = lifecycle;
    this.workspace = workspace;
    this.actions = actions;
    this.queueMutationChains = new Map();
    this.queueServerRevisions = new Map();
    this.queueMutationGeneration = new Map();
    this.queuePendingMutations = new Map();
  }

  retire() {
    this.queueMutationChains.clear();
    this.queueServerRevisions.clear();
    this.queueMutationGeneration.clear();
    this.queuePendingMutations.clear();
  }

  mergeSummary(current, incoming) {
    return mergeServerSessionSummary(current, incoming, this.queueServerRevisions);
  }

  installLists(state, current, archived) {
    return installSessionLists(state, current, archived, this.queueServerRevisions);
  }

  rememberRevision(sessionID, revision) {
    this.queueServerRevisions.set(sessionID, revision);
  }

  async loadQueue(sessionID, owns) {
    const generation = this.queueMutationGeneration.get(sessionID) || 0;
    const response = await api.queue(sessionID);
    if (owns()) this.installAuthoritativeQueue(sessionID, response.data, generation);
  }

  async refreshInputs(sessionID) {
    if (!store.getState().capabilities?.features?.inputLifecycle) return;
    const epoch = this.lifecycle.epoch;
    const response = await api.inputs(sessionID);
    if (this.lifecycle.owns(epoch)) store.setState((state) => installInputs(state, sessionID, response.data));
  }

  async resumeQueue(sessionID) {
    const epoch = this.lifecycle.epoch;
    await api.resumeQueue(sessionID);
    if (this.lifecycle.owns(epoch)) await this.refreshInputs(sessionID);
  }

  async refreshQueue(sessionID) {
    if (!store.getState().sessionData[sessionID]?.loaded) return;
    const generation = this.queueMutationGeneration.get(sessionID) || 0;
    const lifecycleEpoch = this.lifecycle.epoch;
    const response = await api.queue(sessionID);
    if (!this.lifecycle.owns(lifecycleEpoch)) return;
    await this.refreshInputs(sessionID);
    if (this.lifecycle.epoch !== lifecycleEpoch) return;
    this.installAuthoritativeQueue(sessionID, response.data, generation);
  }

  installQueue(sessionID, queue, { authoritative = true } = {}) {
    if (authoritative) this.queueServerRevisions.set(sessionID, queue.revision || 0);
    store.setState((state) => {
      const current = state.sessionData[sessionID] || {};
      if (current.inputLifecycle) {
        const inputs = { ...current.inputs };
        if (queue.inputRevision === current.inputRevision) {
          for (const item of queue.items || []) {
            if (inputs[item.id]) inputs[item.id] = { ...inputs[item.id], prompt: item.prompt };
          }
        }
        return installSessionMachine(state, sessionID, { ...current, inputs,
          queue: { ...current.queue, revision: queue.revision, pendingImages: queue.pendingImages } });
      }
      return { ...state,
        sessions: state.sessions[sessionID] ? { ...state.sessions, [sessionID]: { ...state.sessions[sessionID], queue: queueSummary(queue) } } : state.sessions,
        sessionData: { ...state.sessionData, [sessionID]: { ...current, queue } },
      };
    });
  }

  installAuthoritativeQueue(sessionID, queue, requestGeneration = null) {
    const incomingRevision = queue.revision || 0;
    const knownRevision = this.queueServerRevisions.get(sessionID) || 0;
    if (incomingRevision < knownRevision) return;
    this.queueServerRevisions.set(sessionID, incomingRevision);
    const pending = this.queuePendingMutations.get(sessionID) || [];
    let visible = queue;
    for (const mutation of pending) visible = applyQueueOperations(visible, mutation.operations);
    if (pending.length) {
      visible = { ...visible, revision: (queue.revision || 0) + pending.length };
      this.installQueue(sessionID, visible, { authoritative: false });
      return;
    }
    if (
      requestGeneration !== null &&
      (this.queueMutationGeneration.get(sessionID) || 0) !== requestGeneration
    ) return;
    this.installQueue(sessionID, queue);
  }

  async submitPrompt(sessionID, { text, attachments = [], delivery = "steer" }) {
    if (!text.trim() && !attachments.length) return;
    this.workspace.require(sessionID);
    const lifecycleEpoch = this.lifecycle.epoch;
    const before = store.getState();
    const previousSession = before.sessions[sessionID] || null;
    const previousActiveIndex = before.sessionOrder.indexOf(sessionID);
    const previousArchivedIndex = before.archivedOrder.indexOf(sessionID);
    const inputID = `inp_${randomUUID()}`;
    const prompt = { text, attachments: promptAttachments(attachments) };
    const optimistic = {
      id: inputID,
      prompt,
      delivery,
      timeCreated: new Date().toISOString(),
    };
    if (previousSession?.archivedAt) {
      store.setState((state) => {
        const session = state.sessions[sessionID];
        if (!session) return state;
        return installSessionSummary(
          state,
          { ...session, archivedAt: null },
          { moveToFront: true },
        );
      });
    }
    store.setState((state) => {
      const session = state.sessions[sessionID];
      const next = delivery === "steer" && session
        ? installSessionSummary(state, { ...session, time: { ...session.time, lastUserMessage: optimistic.timeCreated } }, { moveToFront: true })
        : state;
      return installSessionMachine(next, sessionID, projectSubmission(next.sessionData[sessionID] || {}, optimistic));
    });
    try {
      const receipt = await api.admitPrompt(sessionID, {
        id: inputID,
        prompt,
        delivery,
        resume: true,
      });
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      store.setState((state) => installSessionMachine(state, sessionID, admitSubmission(state.sessionData[sessionID] || {}, receipt.data)));
      await this.refreshInputs(sessionID);
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      this.actions.schedule(`queue:${sessionID}`, SUMMARY_REFRESH_MS, () => this.actions.refreshQueue(sessionID));
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      store.setState((state) => {
        const current = state.sessionData[sessionID] || {};
        let next = installSessionMachine(state, sessionID, rollbackSubmission(current, inputID));
        if (previousSession && (delivery === "steer" || previousSession.archivedAt)) {
          next = restoreSessionSummary(
            next,
            previousSession,
            previousActiveIndex,
            previousArchivedIndex,
          );
        }
        return next;
      });
      this.workspace.handleError(error);
      await this.actions.refreshQueue(sessionID).catch(() => {});
      throw error;
    }
  }

  async interrupt(sessionID) {
    const lifecycleEpoch = this.lifecycle.epoch;
    const prior = store.getState().active[sessionID] || null;
    const token = {};
    if (isBusy(prior)) store.setState((state) => optimisticRuntime(state, sessionID, { state: "stopping" }, token));
    try {
      const response = await api.interrupt(sessionID);
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      if (!response.data?.interrupted) store.setState((state) => rollbackRuntime(state, sessionID, token, prior));
      return response.data;
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      store.setState((state) => rollbackRuntime(state, sessionID, token, prior));
      throw error;
    }
  }

  interruptSelectedSession() {
    const state = store.getState();
    const sessionID = state.ui.selectedSessionID;
    const runtime = sessionID ? state.active[sessionID] : null;
    if (!sessionID || !runtime?.turnID || runtime.state === "stopping") return false;
    void this.interrupt(sessionID).catch((error) => this.actions.notice(errorMessage(error)));
    return true;
  }

  async compact(sessionID) {
    this.workspace.require(sessionID);
    const lifecycleEpoch = this.lifecycle.epoch;
    const previous = store.getState().active[sessionID] || null;
    const startedAt = new Date().toISOString();
    const token = {};
    store.setState((state) => optimisticRuntime(state, sessionID, { state: "running", turnID: null, startedAt, activity: "Compacting" }, token));
    try {
      const response = await api.compact(sessionID);
      return this.lifecycle.owns(lifecycleEpoch) ? response : null;
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      store.setState((state) => rollbackRuntime(state, sessionID, token, previous));
      throw error;
    }
  }

  async upload(file, sessionID = null) {
    const limit = store.getState().capabilities?.limits?.maxAttachmentBytes;
    if (limit && file.size > limit) throw new Error(`Attachment is larger than the ${Math.round(limit / 1048576)} MB server limit.`);
    const response = await api.upload(file, sessionID);
    return response.data;
  }

  async patchQueue(sessionID, operations) {
    const queue = store.getState().sessionData[sessionID]?.queue;
    if (!queue) return;
    const generation = (this.queueMutationGeneration.get(sessionID) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.queueMutationGeneration.set(sessionID, generation);
    const pending = this.queuePendingMutations.get(sessionID) || [];
    if (!this.queueServerRevisions.has(sessionID)) {
      this.queueServerRevisions.set(
        sessionID,
        Math.max(0, (queue.revision || 0) - pending.length),
      );
    }
    const mutation = { generation, operations, lifecycleEpoch };
    this.queuePendingMutations.set(sessionID, [...pending, mutation]);
    this.installQueue(
      sessionID,
      {
        ...applyQueueOperations(queue, operations),
        revision: (queue.revision || 0) + 1,
      },
      { authoritative: false },
    );

    const prior = this.queueMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      const expectedRevision = this.queueServerRevisions.get(sessionID) || 0;
      try {
        const response = await api.patchQueue(sessionID, { expectedRevision, operations });
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        const remaining = (this.queuePendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        if (remaining.length) this.queuePendingMutations.set(sessionID, remaining);
        else this.queuePendingMutations.delete(sessionID);
        this.installAuthoritativeQueue(sessionID, response.data);
        return response.data;
      } catch (error) {
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        const remaining = (this.queuePendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        if (remaining.length) this.queuePendingMutations.set(sessionID, remaining);
        else this.queuePendingMutations.delete(sessionID);
        try {
          const refreshed = await api.queue(sessionID);
          if (!this.lifecycle.owns(lifecycleEpoch)) return null;
          this.installAuthoritativeQueue(sessionID, refreshed.data);
        } catch {
          // A later SSE/resync will reconcile if the recovery read also fails.
        }
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (error instanceof ApiError && error.code === "queue_revision_conflict") {
          this.actions.notice("Queue changed elsewhere. Refreshed the current order.");
          return null;
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.queueMutationChains.get(sessionID) === chained) {
        this.queueMutationChains.delete(sessionID);
      }
    });
    this.queueMutationChains.set(sessionID, chained);
    return chained;
  }
}

function promptAttachments(items) {
  return items.map((item) => ({ type: "file", uri: item.uri, name: item.name, mime: item.mime }));
}
