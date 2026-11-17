// @ts-check

import { errorMessage } from "../lib/errors.js";
import { ApiError, api } from "../api/client.js";
import { effectiveAssistantPhase, projectedMessageText } from "../lib/messages.js";
import { fetchOlderMessagePage } from "./message-pagination.js";
import { mergeSessionSummary, reducePublicEvent } from "./reducer.js";

import { store } from "./store.js";

/**
 * Owns message and live-tool read generations and transcript reconciliation.
 * Input and human-input owners load their own fenced snapshots.
 */
export class TranscriptController {
  constructor({ lifecycle, workspace, actions, inputs, humanInput }) {
    this.lifecycle = lifecycle;
    this.workspace = workspace;
    this.actions = actions;
    this.inputs = inputs;
    this.humanInput = humanInput;
    this.messageRefreshGeneration = new Map();
    this.liveToolRefreshGeneration = new Map();
  }

  retire() {
    this.messageRefreshGeneration.clear();
    this.liveToolRefreshGeneration.clear();
  }

  async catchUpHistory(sessionID) {
    const lifecycleEpoch = this.lifecycle.epoch;
    let after = store.getState().sessionData[sessionID]?.latestSeq || 0;
    const beforeRevision = store.getState().sessionData[sessionID]?.inputRevision || 0;
    while (true) {
      let response;
      try { response = await api.history(sessionID, after, 200); }
      catch (error) {
        if (error instanceof ApiError && error.status === 404) return;
        throw error;
      }
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      if (!response.data.length && !response.hasMore) break;
      store.setState((state) => {
        let next = state;
        for (const event of response.data) next = reducePublicEvent(next, event);
        return next;
      });
      after = response.data.at(-1)?.durable?.seq || after;
      if (!response.hasMore) break;
    }
    await this.actions.refreshInputs(sessionID);
    if ((store.getState().sessionData[sessionID]?.inputRevision || 0) !== beforeRevision) {
      await this.actions.refreshMessages(sessionID);
    }
  }

  async loadSession(sessionID, { force = false } = {}) {
    const ownsWorkspace = this.workspace.guard();
    const existing = store.getState().sessionData[sessionID];
    if (existing?.loading) return;
    if (existing?.loaded && existing?.messageSnapshotLoaded && !force) return;
    const loadGeneration = (this.messageRefreshGeneration.get(sessionID) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.messageRefreshGeneration.set(sessionID, loadGeneration);
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loading: true } },
    }));
    try {
      await this.actions.refreshInputs(sessionID);
      if (!this.lifecycle.owns(lifecycleEpoch)) return;
      const cachedSession = store.getState().sessions[sessionID] || null;
      const sessionPromise = cachedSession
        ? Promise.resolve({ data: cachedSession })
        : api.getSession(sessionID);
      const messagesPromise = api.messages(sessionID, { limit: 100, order: "desc" });
      const ownsLoad = () => this.lifecycle.epoch === lifecycleEpoch
        && this.messageRefreshGeneration.get(sessionID) === loadGeneration;
      void this.inputs.loadQueue(sessionID, ownsLoad).catch(() => {});
      void this.humanInput.refreshHumanInput(sessionID, ownsLoad).catch(() => {});
      const [session, messages] = await Promise.all([sessionPromise, messagesPromise]);
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      store.setState((state) => {
        const current = state.sessionData[sessionID] || {};
        const loadedMessages = [...messages.data].reverse();
        const activeTurnID = state.active[sessionID]?.turnID ?? null;
        const mergedSession = this.inputs.mergeSummary(
          state.sessions[sessionID],
          ownsWorkspace() ? session.data : state.sessions[sessionID] || session.data,
        );
        return {
          ...mergeSessionSummary(state, mergedSession),
          sessionData: {
            ...state.sessionData,
            [sessionID]: {
              ...current,
              loaded: true,
              loading: false,
              loadError: null,
              messageSnapshotLoaded: true,
              latestSeq: Math.max(current.latestSeq || 0, messages.snapshotSeq || 0),
              messages: loadedMessages,
              livePrompt: reconcileLivePrompt(current.livePrompt, loadedMessages),
              liveAssistants: reconcileLiveAssistants(current.liveAssistants || {}, loadedMessages, activeTurnID),
              liveTools: reconcileLiveTools(current.liveTools || {}, loadedMessages, activeTurnID),
              messageCursor: messages.cursor?.next || null,
            },
          },
        };
      });
      await this.catchUpHistory(sessionID);
    } catch (error) {
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      store.setState((state) => ({
        ...state,
        sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loading: false, loadError: errorMessage(error) } },
      }));
      throw error;
    }
  }

  async loadOlderMessages(sessionID) {
    const data = store.getState().sessionData[sessionID];
    if (!data?.messageCursor || data.loadingOlder) return;
    const startingCursor = data.messageCursor;
    const lifecycleEpoch = this.lifecycle.epoch;
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loadingOlder: true } },
    }));
    try {
      const page = await fetchOlderMessagePage({
        cursor: startingCursor,
        messages: data.messages || [],
        fetchPage: (cursor) => api.messages(sessionID, { limit: 100, order: "desc", cursor }),
        fetchLatest: () => api.messages(sessionID, { limit: 100, order: "desc" }),
      });
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      const currentBeforeApply = store.getState().sessionData[sessionID];
      if (!page.replacementMessages && currentBeforeApply?.messageCursor !== startingCursor) {
        store.setState((state) => ({
          ...state,
          sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loadingOlder: false } },
        }));
        return this.loadOlderMessages(sessionID);
      }
      let addedCount = 0;
      store.setState((state) => ({
        ...state,
        sessionData: (() => {
          const current = state.sessionData[sessionID] || {};
          const base = page.replacementMessages || current.messages || [];
          const baseIDs = new Set(base.map((message) => message?.id).filter(Boolean));
          const uniqueOlder = page.olderMessages.filter((message) => !baseIDs.has(message.id));
          addedCount = uniqueOlder.length;
          const messages = dedupeMessages([...uniqueOlder, ...base]);
          const activeTurnID = state.active[sessionID]?.turnID ?? null;
          const rebased = Boolean(page.replacementMessages);
          return {
            ...state.sessionData,
            [sessionID]: {
              ...current,
              latestSeq: rebased
                ? Math.max(current.latestSeq || 0, page.replacementSnapshotSeq || 0)
                : current.latestSeq,
              messages,
              livePrompt: rebased ? reconcileLivePrompt(current.livePrompt, messages) : current.livePrompt,
              liveAssistants: rebased
                ? reconcileLiveAssistants(current.liveAssistants || {}, messages, activeTurnID)
                : current.liveAssistants,
              liveTools: rebased
                ? reconcileLiveTools(current.liveTools || {}, messages, activeTurnID)
                : current.liveTools,
              failedPrompts: rebased
                ? (current.failedPrompts || []).filter((prompt) => !messagesContainPrompt(messages, prompt))
                : current.failedPrompts,
              messageCursor: page.nextCursor,
              loadingOlder: false,
            },
          };
        })(),
      }));
      return {
        addedCount,
        recoveredCursor: page.recoveredCursor,
        skippedDuplicatePages: page.duplicatePages,
      };
    } catch (error) {
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      store.setState((state) => ({
        ...state,
        sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loadingOlder: false } },
      }));
      throw error;
    }
  }

  async refreshMessages(sessionID) {
    const sessionData = store.getState().sessionData[sessionID];
    if (!sessionData?.loaded || sessionData.loading) return;
    const generation = (this.messageRefreshGeneration.get(sessionID) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.messageRefreshGeneration.set(sessionID, generation);
    const response = await api.messages(sessionID, { limit: 100, order: "desc" });
    if (
      this.lifecycle.epoch !== lifecycleEpoch ||
      this.messageRefreshGeneration.get(sessionID) !== generation
    ) return;
    const latest = [...response.data].reverse();
    store.setState((state) => {
      const current = state.sessionData[sessionID] || {};
      const livePrompt = current.livePrompt;
      const messages = mergeLatestMessageSnapshot(current.messages || [], latest);
      const activeTurnID = state.active[sessionID]?.turnID ?? null;
      return {
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...current,
            messageSnapshotLoaded: true,
            messages,
            liveAssistants: reconcileLiveAssistants(current.liveAssistants || {}, messages, activeTurnID),
            liveTools: reconcileLiveTools(current.liveTools || {}, messages, activeTurnID),
            livePrompt: reconcileLivePrompt(livePrompt, messages),
            failedPrompts: (current.failedPrompts || []).filter(
              (prompt) => !messagesContainPrompt(messages, prompt),
            ),
          },
        },
      };
    });
  }

  async refreshLiveToolCalls(sessionID) {
    const runtime = store.getState().active[sessionID];
    if (!runtime?.turnID) return;
    const turnID = runtime.turnID;
    const generation = (this.liveToolRefreshGeneration.get(sessionID) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.liveToolRefreshGeneration.set(sessionID, generation);
    const response = await api.toolCalls(sessionID, { turnID, limit: 100 });
    if (
      this.lifecycle.epoch !== lifecycleEpoch ||
      this.liveToolRefreshGeneration.get(sessionID) !== generation
    ) return;
    store.setState((state) => {
      const current = state.sessionData[sessionID] || {};
      if (state.active[sessionID]?.turnID !== turnID) return state;
      const merged = mergeLiveToolSnapshot(current, response.data || []);
      const liveTools = reconcileLiveTools(
        merged.liveTools,
        current.messages || [],
        turnID,
      );
      return {
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: { ...current, ...merged, liveTools },
        },
      };
    });
  }

  scheduleLiveReconcile(sessionID) {
    const key = `live-reconcile:${sessionID}`;
    if (this.lifecycle.scheduler.timers.has(key)) return;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.actions.schedule(key, 750, async () => {
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      if (store.getState().active[sessionID]?.state !== "running") return;
      await this.refreshLiveToolCalls(sessionID);
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      const liveTools = Object.values(
        store.getState().sessionData[sessionID]?.liveTools || {},
      );
      if (liveTools.some((tool) => tool.status !== "running")) {
        await this.actions.refreshMessages(sessionID);
        if (this.lifecycle.epoch !== lifecycleEpoch) return;
      }
      this.scheduleLiveReconcile(sessionID);
    });
  }
}

function messagesContainPrompt(messages, livePrompt) {
  return Boolean(
    livePrompt?.id &&
    messages.some((message) => message.inputID === livePrompt.id),
  );
}

function reconcileLivePrompt(livePrompt, messages) {
  return livePrompt && !messagesContainPrompt(messages, livePrompt) ? livePrompt : null;
}

function mergeLatestMessageSnapshot(current, latest) {
  if (!current.length) return latest;
  if (!latest.length) return current;
  const latestIDs = new Set(latest.map((message) => message.id));
  const overlap = current.findIndex((message) => latestIDs.has(message.id));
  if (overlap < 0) return dedupeMessages([...current, ...latest]);
  const older = current.slice(0, overlap).filter((message) => !latestIDs.has(message.id));
  return dedupeMessages([...older, ...latest]);
}

function dedupeMessages(messages) {
  const seen = new Set();
  return messages.filter((message) => {
    if (!message?.id || seen.has(message.id)) return false;
    seen.add(message.id);
    return true;
  });
}

function reconcileLiveAssistants(liveAssistants, messages, activeTurnID = null) {
  const candidates = messages
    .filter((message) => message.type === "assistant")
    .map((message) => ({
      id: message.id,
      phase: effectiveAssistantPhase(message),
      text: projectedMessageText(message),
      time: Date.parse(message.timeCreated || ""),
    }));
  const used = new Set();
  const next = {};
  for (const [key, live] of Object.entries(liveAssistants)) {
    const liveTime = Date.parse(live.timeCreated || "");
    const match = candidates.find((candidate) => {
      if (used.has(candidate.id)) return false;
      if (candidate.phase !== (live.phase || null) || candidate.text !== String(live.content || "")) return false;
      if (!Number.isFinite(liveTime) || !Number.isFinite(candidate.time)) return true;
      return candidate.time >= liveTime - 2000;
    });
    if (match) {
      used.add(match.id);
      continue;
    }
    if (activeTurnID === null || (live.turnID !== null && live.turnID !== activeTurnID)) continue;
    next[key] = live;
  }
  return next;
}

function reconcileLiveTools(liveTools, messages, activeTurnID = null) {
  const persistedResults = new Set(
    messages
      .filter((message) => message.type === "tool" && message.callID)
      .map((message) => message.callID),
  );
  const next = {};
  for (const [callID, live] of Object.entries(liveTools)) {
    if (persistedResults.has(callID)) continue;
    if (activeTurnID === null || (live.turnID !== null && live.turnID !== activeTurnID)) continue;
    next[callID] = live;
  }
  return next;
}

function mergeLiveToolSnapshot(current, calls) {
  const liveTools = { ...(current.liveTools || {}) };
  let sequence = current.liveTimelineSequence || 0;
  for (const call of calls) {
    if (!call?.id) continue;
    const previous = liveTools[call.id];
    if (!previous) sequence += 1;
    liveTools[call.id] = {
      callID: call.id,
      sequence: previous?.sequence ?? sequence,
      status: ["pending", "running"].includes(call.status) ? "running" : call.status,
      name: call.toolName || previous?.name || "tool",
      arguments: call.arguments?.raw || previous?.arguments || "",
      iteration: call.iteration ?? previous?.iteration ?? null,
      turnID: call.turnID ?? previous?.turnID ?? null,
      startedAt: call.time?.started || previous?.startedAt || null,
      endedAt: call.time?.ended || previous?.endedAt || null,
      ok: call.status === "ok" ? true : ["failed", "cancelled"].includes(call.status) ? false : null,
    };
  }
  return { liveTools, liveTimelineSequence: sequence };
}
