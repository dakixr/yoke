// @ts-check

import { errorMessage } from "../lib/errors.js";
import { ApiError, api } from "../api/client.js";
import { SseClient } from "../api/sse.js";
import { readDone, readDrafts, readToken, writeToken } from "./local-state.js";
import { installActiveSnapshot, reducePublicEvent } from "./reducer.js";

import { store } from "./store.js";

const MESSAGE_REFRESH_MS = 180;

const SUMMARY_REFRESH_MS = 250;

/**
 * Owns stream identity, event buffering and bootstrap/resync latches.
 * Actions refresh other owners through the facade; lifecycle retires requests.
 */
export class ConnectionController {
  constructor({ lifecycle, workspace, actions, inputs }) {
    this.lifecycle = lifecycle;
    this.workspace = workspace;
    this.actions = actions;
    this.inputs = inputs;
    this.sse = null;
    this.bufferEvents = true;
    this.eventBuffer = [];
    this.bootstrapping = false;
    this.resyncing = false;
    this.bootstrapEpoch = null;
    this.resyncEpoch = null;
    this.broadResyncPending = false;
    this.resyncPending = false;
    this.pendingLiveEvents = new Map();
    this.liveFrame = null;
  }

  reset({ stopStream = false } = {}) {
    if (stopStream) this.sse?.stop();
    if (stopStream) this.sse = null;
    this.bootstrapping = false;
    this.resyncing = false;
    this.bootstrapEpoch = null;
    this.resyncEpoch = null;
    this.broadResyncPending = false;
    this.resyncPending = false;
  }

  discardBufferedEvents() {
    this.bufferEvents = false;
    this.eventBuffer = [];
  }

  retire() {
    if (this.liveFrame !== null) cancelAnimationFrame(this.liveFrame);
    this.liveFrame = null;
    this.pendingLiveEvents.clear();
  }

  async start() {
    this.consumeURLToken();
    const token = readToken();
    if (token) api.setToken(token);
    store.setState((state) => ({
      ...state,
      drafts: readDrafts(),
      ui: { ...state.ui, doneUnreviewed: readDone() },
      auth: { ...state.auth, token },
    }));
    window.addEventListener("popstate", this.actions.routeHandler);
    await this.authenticateAndBootstrap();
  }

  stop() {
    window.removeEventListener("popstate", this.actions.routeHandler);
    this.actions.retireLifecycle({ stopStream: true });
  }

  consumeURLToken() {
    const url = new URL(window.location.href);
    const fragment = new URLSearchParams(url.hash.startsWith("#") ? url.hash.slice(1) : url.hash);
    const token = fragment.get("token") || url.searchParams.get("token");
    if (!token) return;
    writeToken(token);
    fragment.delete("token");
    url.searchParams.delete("token");
    const hash = fragment.toString();
    history.replaceState({}, "", `${url.pathname}${url.search}${hash ? `#${hash}` : ""}`);
  }

  async authenticateAndBootstrap(token = readToken()) {
    this.actions.retireLifecycle({ stopStream: true });
    const lifecycleEpoch = this.lifecycle.epoch;
    if (token) {
      api.setToken(token);
      writeToken(token);
    }
    store.setState((state) => ({
      ...state,
      connection: { ...state.connection, status: "connecting", error: null, current: false },
      auth: { ...state.auth, token: token || null },
    }));
    try {
      await api.request("/api/v1/health");
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      await api.capabilities();
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
    } catch (error) {
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      if (error instanceof ApiError && error.status === 401) {
        this.sse?.stop();
        store.setState((state) => ({
          ...state,
          auth: { required: true, token: null },
          connection: { ...state.connection, status: "auth", error: null, current: false },
        }));
        return;
      }
      this.fail(error);
      return;
    }
    if (this.lifecycle.epoch !== lifecycleEpoch) return;
    store.setState((state) => ({ ...state, auth: { required: false, token: token || null } }));
    await this.bootstrap();
  }

  async setToken(token) {
    api.setToken(token);
    writeToken(token);
    await this.authenticateAndBootstrap(token);
  }

  async bootstrap() {
    if (this.bootstrapping) return;
    this.actions.retireLifecycle({ stopStream: true });
    const lifecycleEpoch = this.lifecycle.epoch;
    this.bootstrapping = true;
    this.bootstrapEpoch = lifecycleEpoch;
    this.bufferEvents = true;
    this.eventBuffer = [];
    this.actions.startSse();
    try {
      void api.providers().then((providers) => {
        if (this.lifecycle.epoch !== lifecycleEpoch) return;
        store.setState((state) => ({ ...state, providers: providers.data }));
      }).catch((error) => {
        if (this.lifecycle.epoch === lifecycleEpoch) this.actions.notice(errorMessage(error));
      });
      const [capabilities, active, commands, recent, current, archived] = await Promise.all([
        api.capabilities(),
        api.activeSessions(),
        api.commands(),
        api.recentLocations(),
        api.listSessions({ archived: false, limit: 100, order: "lastUserDesc" }),
        api.listSessions({ archived: true, limit: 30, order: "lastUserDesc" }),
      ]);
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      store.setState((state) => {
        const next = {
          ...this.inputs.installLists(state, current, archived),
          capabilities: capabilities.data,
          commands: commands.data,
          recentLocations: recent.data,
        };
        return installActiveSnapshot(next, active.data);
      });
      this.drainBufferedEvents();
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      this.bufferEvents = false;
      this.eventBuffer = [];
      void this.actions.resolveVisibleLocations();
      void this.actions.refreshProcessLocalState();
      await this.actions.applyRoute();
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      this.actions.persistDone();
      store.setState((state) => ({
        ...state,
        connection: { ...state.connection, status: "connected", current: true, error: null },
      }));
    } catch (error) {
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      this.actions.retireLifecycle({ stopStream: true });
      if (error instanceof ApiError && error.status === 401) {
        store.setState((state) => ({ ...state, auth: { required: true, token: null } }));
      } else {
        this.fail(error);
      }
    } finally {
      if (this.bootstrapEpoch === lifecycleEpoch) {
        this.bootstrapping = false;
        this.bootstrapEpoch = null;
        this.startPendingBroadResync();
      }
    }
  }

  startSse() {
    this.sse?.stop();
    const client = new SseClient({
      headers: () => api.headers(),
      onEvent: (event) => {
        if (this.sse === client) this.receiveEvent(event);
      },
      onState: (status, error) => {
        if (this.sse === client) this.onStreamState(status, error);
      },
    });
    this.sse = client;
    client.start();
  }

  onStreamState(status, error) {
    if (status === "connected") return;
    if (status === "connecting" && store.getState().connection.current) return;
    store.setState((state) => ({
      ...state,
      connection: {
        ...state.connection,
        status,
        current: false,
        error: error || state.connection.error,
      },
    }));
  }

  receiveEvent(event) {
    if (this.bufferEvents) {
      this.eventBuffer.push(event);
      return;
    }
    if (!event.durable && ["session.compaction.delta", "session.context.updated"].includes(event.type)) {
      this.pendingLiveEvents.set(liveEventCoalesceKey(event), event);
      if (this.liveFrame === null) {
        const lifecycleEpoch = this.lifecycle.epoch;
        this.liveFrame = requestAnimationFrame(() => {
          if (this.lifecycle.epoch !== lifecycleEpoch) return;
          const events = [...this.pendingLiveEvents.values()];
          this.pendingLiveEvents.clear();
          this.liveFrame = null;
          this.applyEvents(events);
        });
      }
      return;
    }
    this.applyEvents([event]);
  }

  applyEvents(events) {
    const applied = [];
    store.setState((state) => {
      let next = state;
      for (const event of events) {
        const priorInstance = next.connection.serverInstanceID;
        next = reducePublicEvent(next, event);
        applied.push({ event, priorInstance });
      }
      return next;
    });
    for (const item of applied) this.reconcileAppliedEvent(item.event, item.priorInstance);
  }

  drainBufferedEvents() {
    const events = this.eventBuffer;
    this.eventBuffer = [];
    if (events.length) this.applyEvents(events);
  }

  reconcileAppliedEvent(event, priorInstance) {
    if (event.type === "server.connected") {
      const instance = event.data?.serverInstanceID || null;
      if (priorInstance && instance && priorInstance !== instance) {
        this.actions.retireLifecycle();
        this.requestResync(true);
      } else if (!store.getState().connection.current && !this.bootstrapping) {
        this.requestResync(false);
      }
      return;
    }
    if (event.type === "server.resyncRequired") {
      this.requestResync(true);
      return;
    }
    this.scheduleEventRefresh(event);
    this.actions.persistDone();
  }

  requestResync(broad) {
    if (this.bootstrapping || this.resyncing) {
      this.resyncPending = true;
      if (broad) this.broadResyncPending = true;
      return;
    }
    void this.actions.resync(broad);
  }

  startPendingBroadResync() {
    if ((!this.resyncPending && !this.broadResyncPending) || this.bootstrapping || this.resyncing) return;
    const broad = this.broadResyncPending;
    this.resyncPending = false;
    this.broadResyncPending = false;
    void this.actions.resync(broad);
  }

  async resync(broad = false) {
    if (this.resyncing || this.bootstrapping) {
      this.resyncPending = true;
      if (broad) this.broadResyncPending = true;
      return;
    }
    const lifecycleEpoch = this.lifecycle.epoch;
    this.resyncing = true;
    this.resyncEpoch = lifecycleEpoch;
    this.bufferEvents = true;
    this.eventBuffer = [];
    store.setState((state) => ({
      ...state,
      connection: { ...state.connection, current: false, status: "resyncing" },
    }));
    try {
      const previouslyActive = Object.keys(store.getState().active);
      const [active] = await Promise.all([api.activeSessions(), this.actions.refreshSessionLists()]);
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      store.setState((state) => installActiveSnapshot(state, active.data));
      const relevant = new Set([
        ...Object.keys(store.getState().sessionData),
        ...previouslyActive,
        ...Object.keys(active.data),
      ]);
      const selected = store.getState().ui.selectedSessionID;
      if (selected) relevant.add(selected);
      if (selected) await this.actions.loadSession(selected, { force: true });
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      for (const id of relevant) {
        if (id === selected) continue;
        await this.actions.catchUpHistory(id);
        if (this.lifecycle.epoch !== lifecycleEpoch) return;
      }
      await this.actions.refreshProcessLocalState();
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      if (broad) {
        for (const id of relevant) {
          await Promise.allSettled([
            this.actions.refreshQueue(id),
            this.actions.refreshTree(id),
            this.actions.refreshProcesses(id),
          ]);
          if (this.lifecycle.epoch !== lifecycleEpoch) return;
        }
      }
      this.drainBufferedEvents();
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      store.setState((state) => ({
        ...state,
        connection: { ...state.connection, current: true, status: "connected", error: null },
      }));
      this.actions.persistDone();
      this.actions.notice(broad ? "Reconnected. State synchronized." : "Connection restored. State synchronized.");
    } catch (error) {
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      this.drainBufferedEvents();
      if (this.lifecycle.epoch !== lifecycleEpoch) return;
      this.fail(error);
    } finally {
      if (this.resyncEpoch === lifecycleEpoch) {
        this.bufferEvents = false;
        this.eventBuffer = [];
        this.resyncing = false;
        this.resyncEpoch = null;
        this.startPendingBroadResync();
      }
    }
  }

  async refreshProcessLocalState() {
    const lifecycleEpoch = this.lifecycle.epoch;
    const state = store.getState();
    const attentionIDs = Object.entries(state.attention)
      .filter(([, value]) => (value?.permissions || 0) + (value?.questions || 0) > 0)
      .map(([id]) => id);
    const inspectedIDs = Object.entries(state.sessionData)
      .filter(([, value]) => value?.permissions || value?.questions || value?.processes)
      .map(([id]) => id);
    const ids = new Set([
      ...Object.keys(state.active),
      ...attentionIDs,
      ...inspectedIDs,
      state.ui.selectedSessionID,
    ].filter(Boolean));
    for (const id of ids) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return;
      if (state.active[id]?.state === "running") this.actions.scheduleLiveReconcile(id);
      await Promise.allSettled([
        this.actions.refreshHumanInput(id),
        this.actions.refreshProcesses(id),
        this.actions.refreshLiveToolCalls(id),
      ]);
      if (!this.lifecycle.owns(lifecycleEpoch)) return;
    }
  }

  fail(error) {
    store.setState((state) => ({
      ...state,
      connection: { ...state.connection, status: "error", current: false, error: errorMessage(error) },
    }));
  }

  scheduleEventRefresh(event) {
    const id = event.sessionID;
    if (!id) return;
    if (event.type === "session.workspace.relocated") {
      this.workspace.relocated(event, SUMMARY_REFRESH_MS);
    }
    if (["session.created", "session.updated"].includes(event.type)) {
      this.actions.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshSessionSummary(id));
      this.actions.schedule("lists", SUMMARY_REFRESH_MS, () => this.actions.refreshSessionLists());
    }
    if (store.getState().sessionData[id]?.resyncPending) this.requestResync(false);
    if (event.type.startsWith("session.prompt.") || event.type === "session.queue.updated") {
      this.actions.schedule(`queue:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshQueue(id));
      this.actions.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshSessionSummary(id));
    }
    if (event.type === "session.message.updated") {
      this.actions.schedule(
        `messages:${id}`,
        event.durable ? 0 : MESSAGE_REFRESH_MS,
        () => this.actions.refreshMessages(id),
      );
      this.actions.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshSessionSummary(id));
    }
    if (event.type === "session.active.changed" && event.data?.state === "running") {
      this.actions.schedule(`live-tools:${id}`, 100, () => this.actions.refreshLiveToolCalls(id));
      this.actions.scheduleLiveReconcile(id);
    }
    if (event.type === "session.tool.started" || event.type === "session.tool.ended") {
      this.actions.schedule(`live-tools:${id}`, 80, () => this.actions.refreshLiveToolCalls(id));
      if (store.getState().ui.inspector?.mode === "tool") {
        this.actions.schedule(`tool-inspector:${id}`, 100, async () => {
          const lifecycleEpoch = this.lifecycle.epoch;
          await this.actions.listToolCalls(id);
          if (!this.lifecycle.owns(lifecycleEpoch)) return;
          const inspector = store.getState().ui.inspector;
          const callID = inspector?.mode === "tool" ? inspector.callID : null;
          if (callID) await this.actions.loadToolCall(id, callID);
        });
      }
    }
    if (event.type === "session.tool.ended") {
      this.actions.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.actions.refreshMessages(id));
    }
    if (event.type === "session.interrupted") {
      this.actions.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.actions.refreshMessages(id));
    }
    if (event.type === "session.runtime.failed") {
      this.actions.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.actions.refreshMessages(id));
      this.actions.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshSessionSummary(id));
    }
    if (event.type === "session.active.changed" && (event.data?.error || event.data?.state === "error")) {
      this.actions.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshSessionSummary(id));
    }
    if (event.type === "session.tree.updated") {
      this.actions.schedule(`tree:${id}`, SUMMARY_REFRESH_MS, () => this.actions.refreshTree(id));
    }
    if (event.type.startsWith("session.permission.") || event.type.startsWith("session.question.")) {
      this.actions.schedule(`human:${id}`, 80, () => this.actions.refreshHumanInput(id));
    }
    if (event.type === "session.process.updated") {
      const processID = store.getState().sessionData[id]?.processDetail?.processID;
      if (processID) {
        const lifecycleEpoch = this.lifecycle.epoch;
        void this.actions.refreshProcessOutput(processID).catch((error) => {
          if (this.lifecycle.owns(lifecycleEpoch)) this.actions.notice(errorMessage(error));
        });
      }
      this.actions.throttle(`process-list:${id}`, 750, () => this.actions.refreshProcesses(id));
    }
    if (event.type === "session.selection.changed") {
      this.actions.schedule(`summary:${id}`, 120, () => this.actions.refreshSessionSummary(id));
    }
    if (event.type === "session.skill.activated") {
      this.actions.schedule(`skills:${id}`, 120, () => this.actions.refreshSkills(id));
    }
    if (event.type === "session.tool.config.changed") {
      this.actions.schedule(`tools:${id}`, 120, () => this.actions.refreshTools(id));
    }
    if (event.type === "session.mcp.updated") {
      this.actions.schedule(`mcp:${id}`, 120, () => this.actions.refreshMcp(id));
    }
  }
}

function liveEventCoalesceKey(event) {
  return `${event.sessionID || "global"}:${event.type}`;
}
