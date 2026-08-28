// @ts-check

import { ApiError, api } from "../api/client.js";
import { SseClient } from "../api/sse.js";
import { randomUUID } from "../lib/id.js";
import { currentRoute, draftPath, navigate, sessionPath } from "../router/router.js";
import { effectiveAssistantPhase, projectedMessageText } from "../lib/messages.js";
import {
  readDone,
  readDrafts,
  readReviewed,
  readToken,
  writeDone,
  writeDrafts,
  writeReviewed,
  writeToken,
} from "./local-state.js";
import { installActiveSnapshot, mergeSessionSummary, reducePublicEvent } from "./reducer.js";
import { store } from "./store.js";

const MESSAGE_REFRESH_MS = 180;
const SUMMARY_REFRESH_MS = 250;
const TREE_PAGE_SIZE = 80;

class AppController {
  constructor() {
    this.sse = null;
    this.bufferEvents = true;
    this.eventBuffer = [];
    this.bootstrapping = false;
    this.resyncing = false;
    this.refreshTimers = new Map();
    this.messageRefreshGeneration = new Map();
    this.liveToolRefreshGeneration = new Map();
    this.optimisticSessionGeneration = new Map();
    this.sessionMutationChains = new Map();
    this.sessionPendingMutations = new Map();
    this.queueMutationChains = new Map();
    this.queueServerRevisions = new Map();
    this.queueMutationGeneration = new Map();
    this.queuePendingMutations = new Map();
    this.humanInputGeneration = new Map();
    this.selectionGeneration = new Map();
    this.selectionMutationChains = new Map();
    this.pendingSelections = new Map();
    this.toolMutationChains = new Map();
    this.toolMutationGeneration = new Map();
    this.mcpMutationChains = new Map();
    this.mcpMutationGeneration = new Map();
    this.treeMutationChains = new Map();
    this.treeMutationGeneration = new Map();
    this.treePendingLabels = new Map();
    this.treeServerRevisions = new Map();
    this.pendingLiveEvents = new Map();
    this.liveFrame = null;
    this.routeHandler = () => void this.applyRoute();
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
    window.addEventListener("popstate", this.routeHandler);
    await this.authenticateAndBootstrap();
  }

  stop() {
    this.sse?.stop();
    window.removeEventListener("popstate", this.routeHandler);
    for (const timer of this.refreshTimers.values()) clearTimeout(timer);
    this.refreshTimers.clear();
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
      await api.capabilities();
    } catch (error) {
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
    this.bootstrapping = true;
    this.bufferEvents = true;
    this.eventBuffer = [];
    this.startSse();
    try {
      const providerPromise = api.providers().then((providers) => {
        store.setState((state) => ({ ...state, providers: providers.data }));
        return providers;
      });
      const [capabilities, active, commands, recent, current, archived] = await Promise.all([
        api.capabilities(),
        api.activeSessions(),
        api.commands(),
        api.recentLocations(),
        api.listSessions({ archived: false, limit: 100 }),
        api.listSessions({ archived: true, limit: 30 }),
      ]);
      store.setState((state) => {
        const sessions = { ...state.sessions };
        for (const session of [...current.data, ...archived.data]) sessions[session.id] = session;
        let next = {
          ...state,
          capabilities: capabilities.data,
          sessions,
          sessionOrder: current.data.map((item) => item.id),
          archivedOrder: archived.data.map((item) => item.id),
          sessionsCursor: current.cursor?.next || null,
          archivedCursor: archived.cursor?.next || null,
          commands: commands.data,
          recentLocations: recent.data,
        };
        next = installActiveSnapshot(next, active.data);
        for (const event of this.eventBuffer) next = reducePublicEvent(next, event);
        return next;
      });
      this.bufferEvents = false;
      this.eventBuffer = [];
      void providerPromise.catch((error) => this.notice(errorMessage(error)));
      void this.resolveVisibleLocations();
      void this.refreshProcessLocalState();
      await this.applyRoute();
      this.persistDone();
      store.setState((state) => ({
        ...state,
        connection: { ...state.connection, status: "connected", current: true, error: null },
      }));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        store.setState((state) => ({ ...state, auth: { required: true, token: null } }));
      } else {
        this.fail(error);
      }
    } finally {
      this.bootstrapping = false;
    }
  }

  startSse() {
    this.sse?.stop();
    this.sse = new SseClient({
      headers: () => api.headers(),
      onEvent: (event) => this.receiveEvent(event),
      onState: (status, error) => this.onStreamState(status, error),
    });
    this.sse.start();
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
        this.liveFrame = requestAnimationFrame(() => {
          const events = [...this.pendingLiveEvents.values()];
          this.pendingLiveEvents.clear();
          this.liveFrame = null;
          store.setState((state) => events.reduce((next, item) => reducePublicEvent(next, item), state));
          for (const item of events) this.scheduleEventRefresh(item);
        });
      }
      return;
    }
    const priorInstance = store.getState().connection.serverInstanceID;
    store.setState((state) => reducePublicEvent(state, event));
    if (event.type === "server.connected") {
      const instance = event.data?.serverInstanceID || null;
      if (priorInstance && instance && priorInstance !== instance) void this.resync(true);
      else if (!store.getState().connection.current) void this.resync(false);
      return;
    }
    if (event.type === "server.resyncRequired") {
      void this.resync(true);
      return;
    }
    this.scheduleEventRefresh(event);
    this.persistDone();
  }

  scheduleEventRefresh(event) {
    const id = event.sessionID;
    if (!id) return;
    if (["session.created", "session.updated"].includes(event.type)) {
      this.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.refreshSessionSummary(id));
      this.schedule("lists", SUMMARY_REFRESH_MS, () => this.refreshSessionLists());
    }
    if (event.type.startsWith("session.prompt.") || event.type === "session.queue.updated") {
      this.schedule(`queue:${id}`, SUMMARY_REFRESH_MS, () => this.refreshQueue(id));
      this.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.refreshSessionSummary(id));
    }
    if (event.type === "session.message.updated") {
      this.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.refreshMessages(id));
      this.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.refreshSessionSummary(id));
    }
    if (event.type === "session.active.changed" && event.data?.state === "running") {
      this.schedule(`live-tools:${id}`, 100, () => this.refreshLiveToolCalls(id));
      this.scheduleLiveReconcile(id);
    }
    if (event.type === "session.tool.started" || event.type === "session.tool.ended") {
      this.schedule(`live-tools:${id}`, 80, () => this.refreshLiveToolCalls(id));
    }
    if (event.type === "session.tool.ended") {
      this.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.refreshMessages(id));
    }
    if (event.type === "session.interrupted") {
      this.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.refreshMessages(id));
    }
    if (event.type === "session.runtime.failed") {
      this.schedule(`messages:${id}`, MESSAGE_REFRESH_MS, () => this.refreshMessages(id));
      this.schedule(`summary:${id}`, SUMMARY_REFRESH_MS, () => this.refreshSessionSummary(id));
    }
    if (event.type === "session.tree.updated") {
      this.schedule(`tree:${id}`, SUMMARY_REFRESH_MS, () => this.refreshTree(id));
    }
    if (event.type.startsWith("session.permission.") || event.type.startsWith("session.question.")) {
      this.schedule(`human:${id}`, 80, () => this.refreshHumanInput(id));
    }
    if (event.type === "session.process.updated") {
      this.schedule(`process:${id}`, 120, async () => {
        await this.refreshProcesses(id);
        const processID = store.getState().sessionData[id]?.processDetail?.processID;
        if (processID) await this.loadProcess(processID);
      });
    }
    if (event.type === "session.selection.changed") {
      this.schedule(`summary:${id}`, 120, () => this.refreshSessionSummary(id));
    }
    if (event.type === "session.skill.activated") {
      this.schedule(`skills:${id}`, 120, () => this.refreshSkills(id));
    }
    if (event.type === "session.tool.config.changed") {
      this.schedule(`tools:${id}`, 120, () => this.refreshTools(id));
    }
    if (event.type === "session.mcp.updated") {
      this.schedule(`mcp:${id}`, 120, () => this.refreshMcp(id));
    }
  }

  schedule(key, delay, task) {
    clearTimeout(this.refreshTimers.get(key));
    const timer = setTimeout(async () => {
      this.refreshTimers.delete(key);
      try { await task(); } catch (error) { this.notice(errorMessage(error)); }
    }, delay);
    this.refreshTimers.set(key, timer);
  }

  scheduleLiveReconcile(sessionID) {
    const key = `live-reconcile:${sessionID}`;
    if (this.refreshTimers.has(key)) return;
    this.schedule(key, 750, async () => {
      if (store.getState().active[sessionID]?.state !== "running") return;
      await this.refreshLiveToolCalls(sessionID);
      const liveTools = Object.values(
        store.getState().sessionData[sessionID]?.liveTools || {},
      );
      if (liveTools.some((tool) => tool.status !== "running")) {
        await this.refreshMessages(sessionID);
      }
      this.scheduleLiveReconcile(sessionID);
    });
  }

  async resync(broad = false) {
    if (this.resyncing || this.bootstrapping) return;
    this.resyncing = true;
    this.bufferEvents = true;
    this.eventBuffer = [];
    store.setState((state) => ({
      ...state,
      connection: { ...state.connection, current: false, status: "resyncing" },
    }));
    try {
      const [active] = await Promise.all([api.activeSessions(), this.refreshSessionLists()]);
      store.setState((state) => installActiveSnapshot(state, active.data));
      const relevant = new Set([
        ...Object.keys(store.getState().sessionData),
        ...Object.keys(active.data),
      ]);
      const selected = store.getState().ui.selectedSessionID;
      if (selected) relevant.add(selected);
      for (const id of relevant) await this.catchUpHistory(id);
      if (selected) await this.loadSession(idOr(selected), { force: true });
      await this.refreshProcessLocalState();
      if (broad) {
        for (const id of relevant) {
          await Promise.allSettled([
            this.refreshQueue(id),
            this.refreshTree(id),
            this.refreshProcesses(id),
          ]);
        }
      }
      store.setState((state) => {
        let next = state;
        for (const event of this.eventBuffer) next = reducePublicEvent(next, event);
        return { ...next, connection: { ...next.connection, current: true, status: "connected", error: null } };
      });
      this.persistDone();
      this.notice(broad ? "Reconnected. State synchronized." : "Connection restored. State synchronized.");
    } catch (error) {
      this.fail(error);
    } finally {
      this.bufferEvents = false;
      this.eventBuffer = [];
      this.resyncing = false;
    }
  }

  async catchUpHistory(sessionID) {
    let after = store.getState().sessionData[sessionID]?.latestSeq || 0;
    while (true) {
      let response;
      try { response = await api.history(sessionID, after, 200); }
      catch (error) {
        if (error instanceof ApiError && error.status === 404) return;
        throw error;
      }
      if (!response.data.length && !response.hasMore) return;
      store.setState((state) => {
        let next = state;
        for (const event of response.data) next = reducePublicEvent(next, event);
        return next;
      });
      after = response.data.at(-1)?.durable?.seq || after;
      if (!response.hasMore) return;
    }
  }

  async refreshSessionLists() {
    const [current, archived] = await Promise.all([
      api.listSessions({ archived: false, limit: 100 }),
      api.listSessions({ archived: true, limit: 30 }),
    ]);
    store.setState((state) => {
      const sessions = { ...state.sessions };
      for (const item of [...current.data, ...archived.data]) sessions[item.id] = item;
      let next = {
        ...state,
        sessions,
        sessionOrder: current.data.map((item) => item.id),
        archivedOrder: archived.data.map((item) => item.id),
        sessionsCursor: current.cursor?.next || null,
        archivedCursor: archived.cursor?.next || null,
      };
      for (const [sessionID, mutations] of this.sessionPendingMutations) {
        if (!mutations.length) continue;
        const base = next.sessions[sessionID] || state.sessions[sessionID];
        if (!base) continue;
        let visible = base;
        for (const mutation of mutations) visible = optimisticSessionPatch(visible, mutation.patch);
        next = installSessionSummary(next, visible, { moveToFront: true });
      }
      for (const [sessionID, selection] of this.pendingSelections) {
        const session = next.sessions[sessionID];
        if (!session) continue;
        next = {
          ...next,
          sessions: {
            ...next.sessions,
            [sessionID]: { ...session, selection },
          },
        };
      }
      return next;
    });
    void this.resolveVisibleLocations();
  }

  async loadMoreSessions(archived = false) {
    const state = store.getState();
    const cursor = archived ? state.archivedCursor : state.sessionsCursor;
    if (!cursor) return;
    const response = await api.listSessions({ archived, limit: archived ? 30 : 100, cursor });
    store.setState((current) => {
      const sessions = { ...current.sessions };
      for (const item of response.data) sessions[item.id] = item;
      const key = archived ? "archivedOrder" : "sessionOrder";
      return {
        ...current,
        sessions,
        [key]: [...current[key], ...response.data.map((item) => item.id)],
        [archived ? "archivedCursor" : "sessionsCursor"]: response.cursor?.next || null,
      };
    });
    void this.resolveVisibleLocations();
  }

  async refreshSessionSummary(sessionID) {
    const response = await api.getSession(sessionID);
    store.setState((state) => {
      let summary = response.data;
      for (const mutation of this.sessionPendingMutations.get(sessionID) || []) {
        summary = optimisticSessionPatch(summary, mutation.patch);
      }
      const selection = this.pendingSelections.get(sessionID);
      if (selection) summary = { ...summary, selection };
      return mergeSessionSummary(state, summary);
    });
  }

  async resolveVisibleLocations() {
    const state = store.getState();
    const directories = [...new Set(
      [...state.sessionOrder, ...state.archivedOrder]
        .map((id) => state.sessions[id]?.location?.directory)
        .filter(Boolean),
    )].filter((directory) => !state.locations[directory]);
    let next = 0;
    const worker = async () => {
      while (next < directories.length) {
        const directory = directories[next++];
        if (store.getState().locations[directory]) continue;
        try {
          const response = await api.resolveLocation(directory);
          store.setState((current) => ({
            ...current,
            locations: { ...current.locations, [directory]: response.data },
          }));
        } catch {
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
    const value = query.trim();
    store.setState((state) => ({ ...state, ui: { ...state.ui, search: query, searching: Boolean(value) } }));
    if (!value) {
      store.setState((state) => ({ ...state, ui: { ...state.ui, searchResults: [], searching: false } }));
      return;
    }
    const response = await api.listSessions({ search: value, limit: 100 });
    store.setState((state) => {
      const sessions = { ...state.sessions };
      for (const item of response.data) sessions[item.id] = item;
      return {
        ...state,
        sessions,
        ui: { ...state.ui, searchResults: response.data.map((item) => item.id), searching: false },
      };
    });
    void this.resolveVisibleLocations();
  }

  async applyRoute() {
    const route = currentRoute();
    if (route.name === "session") {
      this.selectSession(route.sessionID, { navigate: false });
      await this.loadSession(route.sessionID);
      return;
    }
    if (route.name === "new") {
      const id = route.draftID || this.createDraft({ navigate: false });
      if (!route.draftID) navigate(draftPath(id), { replace: true });
      store.setState((state) => ({
        ...state,
        ui: { ...state.ui, selectedSessionID: null, newSession: true, sidebarOpen: window.innerWidth > 760 },
      }));
      return;
    }
    if (route.name === "home") {
      const first = store.getState().sessionOrder[0];
      if (first) {
        history.replaceState({}, "", sessionPath(first));
        this.selectSession(first, { navigate: false });
        await this.loadSession(first);
      } else {
        const id = this.createDraft({ navigate: false });
        history.replaceState({}, "", draftPath(id));
        store.setState((state) => ({
          ...state,
          ui: {
            ...state.ui,
            selectedSessionID: null,
            newSession: true,
            sidebarOpen: window.innerWidth > 760,
          },
        }));
      }
    }
  }

  selectSession(sessionID, { navigate: shouldNavigate = true } = {}) {
    this.clearNotice();
    const done = { ...store.getState().ui.doneUnreviewed, [sessionID]: false };
    const reviewed = readReviewed();
    reviewed[sessionID] = new Date().toISOString();
    writeReviewed(reviewed);
    writeDone(done);
    store.setState((state) => ({
      ...state,
      ui: {
        ...state.ui,
        selectedSessionID: sessionID,
        newSession: false,
        sidebarOpen: window.innerWidth > 760 ? state.ui.sidebarOpen : false,
        doneUnreviewed: done,
      },
    }));
    if (shouldNavigate) navigate(sessionPath(sessionID));
  }

  async loadSession(sessionID, { force = false } = {}) {
    const existing = store.getState().sessionData[sessionID];
    if (existing?.loading) return;
    if (existing?.loaded && !force) return;
    const loadGeneration = (this.messageRefreshGeneration.get(sessionID) || 0) + 1;
    this.messageRefreshGeneration.set(sessionID, loadGeneration);
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loading: true } },
    }));
    try {
      const cachedSession = store.getState().sessions[sessionID] || null;
      const sessionPromise = cachedSession
        ? Promise.resolve({ data: cachedSession })
        : api.getSession(sessionID);
      const messagesPromise = api.messages(sessionID, { limit: 100, order: "desc" });
      const queueGeneration = this.queueMutationGeneration.get(sessionID) || 0;
      const queuePromise = api.queue(sessionID).then((response) => {
        if (this.messageRefreshGeneration.get(sessionID) === loadGeneration) {
          this.installAuthoritativeQueue(sessionID, response.data, queueGeneration);
        }
      }).catch(() => {});
      const humanGeneration = this.humanInputGeneration.get(sessionID) || 0;
      const humanInputPromise = Promise.all([
        api.permissions(sessionID),
        api.questions(sessionID),
      ]).then(([permissions, questions]) => {
        if (this.messageRefreshGeneration.get(sessionID) !== loadGeneration) return;
        if ((this.humanInputGeneration.get(sessionID) || 0) !== humanGeneration) return;
        this.installHumanInput(sessionID, permissions.data, questions.data);
      }).catch(() => {});
      const [session, messages] = await Promise.all([sessionPromise, messagesPromise]);
      store.setState((state) => {
        const current = state.sessionData[sessionID] || {};
        const loadedMessages = [...messages.data].reverse();
        const activeTurnID = state.active[sessionID]?.turnID ?? null;
        return {
          ...mergeSessionSummary(state, session.data),
          sessionData: {
            ...state.sessionData,
            [sessionID]: {
              ...current,
              loaded: true,
              loading: false,
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
      void Promise.all([queuePromise, humanInputPromise]);
      await this.catchUpHistory(sessionID);
    } catch (error) {
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
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loadingOlder: true } },
    }));
    try {
      const response = await api.messages(sessionID, { limit: 100, order: "desc", cursor: data.messageCursor });
      store.setState((state) => ({
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...state.sessionData[sessionID],
            messages: dedupeMessages([
              ...[...response.data].reverse(),
              ...(state.sessionData[sessionID]?.messages || []),
            ]),
            messageCursor: response.cursor?.next || null,
            loadingOlder: false,
          },
        },
      }));
    } catch (error) {
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
    this.messageRefreshGeneration.set(sessionID, generation);
    const response = await api.messages(sessionID, { limit: 100, order: "desc" });
    if (this.messageRefreshGeneration.get(sessionID) !== generation) return;
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
    this.liveToolRefreshGeneration.set(sessionID, generation);
    const response = await api.toolCalls(sessionID, { turnID, limit: 100 });
    if (this.liveToolRefreshGeneration.get(sessionID) !== generation) return;
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

  async refreshQueue(sessionID) {
    if (!store.getState().sessionData[sessionID]?.loaded) return;
    const generation = this.queueMutationGeneration.get(sessionID) || 0;
    const response = await api.queue(sessionID);
    this.installAuthoritativeQueue(sessionID, response.data, generation);
  }

  async refreshHumanInput(sessionID) {
    const generation = this.humanInputGeneration.get(sessionID) || 0;
    const [permissions, questions] = await Promise.all([api.permissions(sessionID), api.questions(sessionID)]);
    if ((this.humanInputGeneration.get(sessionID) || 0) !== generation) return;
    this.installHumanInput(sessionID, permissions.data, questions.data);
  }

  async refreshProcessLocalState() {
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
      if (state.active[id]?.state === "running") this.scheduleLiveReconcile(id);
      await Promise.allSettled([
        this.refreshHumanInput(id),
        this.refreshProcesses(id),
        this.refreshLiveToolCalls(id),
      ]);
    }
  }

  async refreshTree(sessionID) {
    if (!store.getState().sessionData[sessionID]?.tree && store.getState().ui.inspector?.mode !== "tree") return;
    const response = await api.tree(sessionID, { limit: TREE_PAGE_SIZE });
    this.treeServerRevisions.set(sessionID, response.data.revision || 0);
    const pending = this.treePendingLabels.get(sessionID) || [];
    const incoming = applyPendingTreeLabels(response.data, pending);
    const current = store.getState().sessionData[sessionID]?.tree;
    if (!current?.entries?.length || current.entries.length <= incoming.entries.length) {
      this.setSessionField(sessionID, "tree", incoming);
      return;
    }
    const latestIDs = new Set(incoming.entries.map((entry) => entry.id));
    const older = current.entries.filter((entry) => !latestIDs.has(entry.id));
    this.setSessionField(sessionID, "tree", applyPendingTreeLabels({
      ...incoming,
      entries: [...older, ...incoming.entries],
      cursor: current.cursor,
    }, pending));
  }

  async loadMoreTree(sessionID) {
    const current = store.getState().sessionData[sessionID]?.tree;
    const cursor = current?.cursor?.next;
    if (!cursor) return;
    const response = await api.tree(sessionID, { limit: TREE_PAGE_SIZE, cursor });
    this.treeServerRevisions.set(sessionID, response.data.revision || 0);
    const existingIDs = new Set(current.entries.map((entry) => entry.id));
    const older = response.data.entries.filter((entry) => !existingIDs.has(entry.id));
    const pending = this.treePendingLabels.get(sessionID) || [];
    this.setSessionField(sessionID, "tree", applyPendingTreeLabels({
      ...current,
      ...response.data,
      entries: [...older, ...current.entries],
      cursor: response.data.cursor,
    }, pending));
  }

  async refreshProcesses(sessionID) {
    if (store.getState().ui.inspector?.mode !== "process" && !store.getState().sessionData[sessionID]?.processes) return;
    const response = await api.processes({ sessionID });
    this.setSessionField(sessionID, "processes", response.data);
  }

  async refreshTools(sessionID) {
    const session = store.getState().sessions[sessionID];
    if (!session) return;
    const response = await api.tools({ directory: session.location.directory, sessionID });
    this.setSessionField(sessionID, "tools", response.data);
  }

  async refreshSkills(sessionID) {
    const response = await api.sessionSkills(sessionID);
    this.setSessionField(sessionID, "skills", response.data);
  }

  async refreshMcp(sessionID) {
    const response = await api.sessionMcp(sessionID, true);
    this.setSessionField(sessionID, "mcp", response.data);
  }

  async loadModels(directory, provider = null, search = null) {
    const response = await api.models({ directory, provider, search });
    store.setState((state) => ({
      ...state,
      models: { ...(state.models || {}), [`${directory || ""}:${provider || ""}:${search || ""}`]: response.data },
    }));
    return response.data;
  }

  async loadProviders(directory = null) {
    const response = await api.providers(directory);
    store.setState((state) => ({
      ...state,
      providerCatalogs: { ...(state.providerCatalogs || {}), [directory || ""]: response.data },
    }));
    return response.data;
  }

  setSessionField(sessionID, key, value) {
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], [key]: value } },
    }));
  }

  installQueue(sessionID, queue, { authoritative = true } = {}) {
    if (authoritative) this.queueServerRevisions.set(sessionID, queue.revision || 0);
    store.setState((state) => ({
      ...state,
      sessions: state.sessions[sessionID]
        ? {
            ...state.sessions,
            [sessionID]: { ...state.sessions[sessionID], queue: queueSummary(queue) },
          }
        : state.sessions,
      sessionData: {
        ...state.sessionData,
        [sessionID]: { ...state.sessionData[sessionID], queue },
      },
    }));
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

  installHumanInput(sessionID, permissions, questions) {
    store.setState((state) => ({
      ...state,
      attention: {
        ...state.attention,
        [sessionID]: { permissions: permissions.length, questions: questions.length },
      },
      sessionData: {
        ...state.sessionData,
        [sessionID]: { ...state.sessionData[sessionID], permissions, questions },
      },
    }));
  }

  createDraft({ navigate: shouldNavigate = true, location = null } = {}) {
    this.clearNotice();
    const id = `draft_${randomUUID()}`;
    const state = store.getState();
    const recent = location || state.recentLocations[0]?.directory || "";
    const defaultProvider = state.providers.find((item) => item.ready && item.currentModel) || null;
    const draft = {
      id,
      text: "",
      location: recent,
      provider: defaultProvider?.id || "",
      model: defaultProvider?.currentModel || "",
      reasoningEffort: defaultProvider?.currentReasoningEffort || "",
      attachments: [],
      updatedAt: new Date().toISOString(),
    };
    store.setState((state) => ({ ...state, drafts: { ...state.drafts, [id]: draft } }));
    if (shouldNavigate) navigate(draftPath(id));
    return id;
  }

  updateDraft(id, patch) {
    const current = store.getState().drafts[id] || { id, attachments: [] };
    const draft = { ...current, ...patch, id, updatedAt: new Date().toISOString() };
    const drafts = { ...store.getState().drafts, [id]: draft };
    store.setState((state) => ({ ...state, drafts }));
    if ((draft.text || "").trim() || draft.attachments?.length) writeDrafts(drafts);
  }

  deleteDraft(id) {
    const drafts = { ...store.getState().drafts };
    delete drafts[id];
    writeDrafts(drafts);
    store.setState((state) => ({ ...state, drafts }));
  }

  async submitDraft(id, { delivery = "steer" } = {}) {
    const draft = store.getState().drafts[id];
    if (!draft) throw new Error("Draft was not found.");
    if (!(draft.text || "").trim() && !draft.attachments?.length) throw new Error("Write a prompt or attach an image first.");
    const sessionID = await this.createSessionFromDraft(id);
    this.selectSession(sessionID);
    try {
      await this.submitPrompt(sessionID, {
        text: draft.text || "",
        attachments: draft.attachments || [],
        delivery,
      });
      this.deleteDraft(id);
    } catch (error) {
      if (draft.text) this.setSessionField(sessionID, "editorHandoff", draft.text);
      throw error;
    }
  }

  async createSessionFromDraft(id, { titleText = null } = {}) {
    const draft = store.getState().drafts[id];
    if (!draft) throw new Error("Draft was not found.");
    const location = draft.location || store.getState().recentLocations[0]?.directory;
    if (!location) throw new Error("Choose a working location first.");
    const sessionID = `web_${randomUUID()}`;
    const selection = draft.provider && draft.model ? {
      provider: draft.provider,
      model: draft.model,
      reasoningEffort: draft.reasoningEffort || null,
    } : undefined;
    const response = await api.createSession({
      id: sessionID,
      location: { directory: location },
      title: provisionalSessionTitle(titleText ?? draft.text),
      selection,
    });
    store.setState((state) => {
      const next = installSessionSummary(state, response.data, { moveToFront: true });
      const revision = response.data.queue?.revision || 0;
      return {
        ...next,
        attention: {
          ...next.attention,
          [sessionID]: { permissions: 0, questions: 0 },
        },
        sessionData: {
          ...next.sessionData,
          [sessionID]: {
            ...next.sessionData[sessionID],
            loaded: true,
            loading: false,
            messages: next.sessionData[sessionID]?.messages || [],
            queue: next.sessionData[sessionID]?.queue || { revision, items: [] },
            permissions: next.sessionData[sessionID]?.permissions || [],
            questions: next.sessionData[sessionID]?.questions || [],
          },
        },
      };
    });
    this.queueServerRevisions.set(sessionID, response.data.queue?.revision || 0);
    return sessionID;
  }

  async finishDraftSession(id, sessionID) {
    this.deleteDraft(id);
    this.selectSession(sessionID);
    await this.loadSession(sessionID, { force: true });
  }

  async slashSkillCompletions(directory, search = "") {
    const response = await api.skills({ directory, search: search || null });
    return response.data || [];
  }

  async slashMcpCompletions(sessionID, directory, search = "") {
    const response = sessionID
      ? await api.sessionMcp(sessionID, false)
      : await api.mcp({ directory, includeTools: false });
    return (response.data || [])
      .filter((server) => !search || String(server.name || "").toLowerCase().includes(search.toLowerCase()))
      .map((server) => ({
        name: server.name,
        description: `${server.transport || "MCP"} · ${server.status || (server.enabled ? "enabled" : "disabled")}`,
      }));
  }

  async runSlashCommand(text, { sessionID = null, draftID = null, directory = "" } = {}) {
    const parsed = parseSlashCommand(text);
    if (!parsed) return { handled: false };
    const { name, args } = parsed;
    if (name === "shortcuts" || name === "?") {
      if (args) return slashUsage(this, name === "?" ? "?" : "/shortcuts");
      this.showShortcutHelp();
      return { handled: true };
    }
    if (name === "image") {
      if (args) {
        this.notice("Browser image attachments use the file picker, drag and drop, or paste. Use /image without a path.");
        return { handled: true, clear: false };
      }
      return { handled: true, action: "image" };
    }
    if (name === "new") {
      if (args) return slashUsage(this, "/new");
      if (draftID) this.deleteDraft(draftID);
      this.createDraft({ location: directory || null });
      return { handled: true };
    }
    if (name === "model") {
      if (args) return slashUsage(this, "/model");
      requestAnimationFrame(() => this.focusModelSelector());
      return { handled: true };
    }
    if (name === "skill") {
      const [skillName, prompt] = splitFirstArgument(args);
      if (!skillName) {
        this.notice("Usage: /skill <name> [prompt]");
        return { handled: true, clear: false };
      }
      if (sessionID) {
        await this.activateSkill(sessionID, skillName, prompt || null);
        await this.refreshQueue(sessionID);
        this.notice(`Activated skill: ${skillName}`);
        return { handled: true };
      }
      if (draftID) {
        const available = await this.slashSkillCompletions(directory, skillName);
        if (!available.some((skill) => skill.name === skillName)) {
          this.notice(`Unknown skill: ${skillName}`);
          return { handled: true, clear: false };
        }
        const createdID = await this.createSessionFromDraft(draftID, {
          titleText: prompt || `Use ${skillName}`,
        });
        try {
          await this.activateSkill(createdID, skillName, prompt || null);
          this.notice(`Activated skill: ${skillName}`);
        } finally {
          await this.finishDraftSession(draftID, createdID);
        }
        return { handled: true };
      }
    }
    if (!sessionID) {
      this.notice(`/${name} is available after a session starts.`);
      return { handled: true, clear: false };
    }
    const session = store.getState().sessions[sessionID];
    if (name === "compact") {
      if (args) return slashUsage(this, "/compact");
      await this.compact(sessionID);
      return { handled: true };
    }
    if (name === "pin") {
      if (args) return slashUsage(this, "/pin");
      await this.patchSession(sessionID, { pinned: !session?.pinned });
      return { handled: true };
    }
    if (name === "info") {
      if (args) return slashUsage(this, "/info");
      await this.openInspector("context");
      return { handled: true };
    }
    if (name === "fork") {
      if (args) return slashUsage(this, "/fork");
      await this.forkSession(sessionID);
      return { handled: true };
    }
    if (name === "title") {
      if (!args) return slashUsage(this, "/title <new-title>");
      await this.patchSession(sessionID, { title: args });
      return { handled: true };
    }
    if (name === "regenerate-title") {
      if (args) return slashUsage(this, "/regenerate-title");
      this.pendingNotice("Regenerating title…");
      const updated = await this.regenerateTitle(sessionID);
      this.notice(`Title updated to “${updated.title}”.`);
      return { handled: true };
    }
    if (name === "tree") {
      if (args) return slashUsage(this, "/tree");
      await this.openInspector("tree");
      return { handled: true };
    }
    if (name === "tools") {
      if (args) return slashUsage(this, "/tools");
      await this.openInspector("tools");
      return { handled: true };
    }
    if (name === "mcp") {
      await this.openInspector("mcp", args ? { serverName: args.split(/\s+/)[0] } : {});
      return { handled: true };
    }
    if (name === "queue") {
      if (args) return slashUsage(this, "/queue");
      this.focusQueueEditor();
      return { handled: true };
    }
    if (name === "ps") {
      if (args) return slashUsage(this, "/ps");
      await this.openInspector("process");
      return { handled: true };
    }
    this.notice(`Unknown command: /${name}`);
    return { handled: true, clear: false };
  }

  async submitPrompt(sessionID, { text, attachments = [], delivery = "steer" }) {
    if (!text.trim() && !attachments.length) return;
    const data = store.getState().sessionData[sessionID];
    if (data?.lastError && data?.livePrompt) {
      await this.refreshMessages(sessionID);
      const reconciled = store.getState().sessionData[sessionID];
      if (reconciled?.lastError && reconciled?.livePrompt) {
        store.setState((state) => {
          const current = state.sessionData[sessionID] || {};
          const failedPrompts = current.failedPrompts || [];
          const failed = current.livePrompt;
          return {
            ...state,
            sessionData: {
              ...state.sessionData,
              [sessionID]: {
                ...current,
                failedPrompts:
                  failed && !failedPrompts.some((item) => item.id === failed.id)
                    ? [...failedPrompts, failed]
                    : failedPrompts,
                livePrompt: null,
              },
            },
          };
        });
      }
    }
    const inputID = `inp_${randomUUID()}`;
    const prompt = { text, attachments: promptAttachments(attachments) };
    const optimistic = {
      id: inputID,
      prompt,
      delivery,
      timeCreated: new Date().toISOString(),
    };
    if (delivery === "steer") {
      store.setState((state) => ({
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...state.sessionData[sessionID],
            livePrompt: optimistic,
            lastError: null,
          },
        },
      }));
    } else {
      const queue = store.getState().sessionData[sessionID]?.queue;
      if (queue) {
        this.installQueue(
          sessionID,
          {
            ...queue,
            items: [
              ...queue.items,
              {
                id: inputID,
                prompt,
                delivery,
                paused: false,
                createdAt: optimistic.timeCreated,
                state: "admitted",
              },
            ],
          },
          { authoritative: false },
        );
      }
    }
    try {
      await api.admitPrompt(sessionID, {
        id: inputID,
        prompt,
        delivery,
        resume: true,
      });
      this.schedule(`queue:${sessionID}`, SUMMARY_REFRESH_MS, () => this.refreshQueue(sessionID));
    } catch (error) {
      const queued = store.getState().sessionData[sessionID]?.queue;
      if (queued?.items?.some((item) => item.id === inputID)) {
        this.installQueue(
          sessionID,
          { ...queued, items: queued.items.filter((item) => item.id !== inputID) },
          { authoritative: false },
        );
      }
      store.setState((state) => {
        const current = state.sessionData[sessionID] || {};
        return {
          ...state,
          sessionData: {
            ...state.sessionData,
            [sessionID]: {
              ...current,
              livePrompt: current.livePrompt?.id === inputID ? null : current.livePrompt,
            },
          },
        };
      });
      await this.refreshQueue(sessionID).catch(() => {});
      throw error;
    }
  }

  async interrupt(sessionID) {
    const prior = store.getState().active[sessionID] || null;
    if (prior?.state && prior.state !== "idle") {
      store.setState((state) => ({
        ...state,
        active: {
          ...state.active,
          [sessionID]: { ...state.active[sessionID], state: "stopping" },
        },
      }));
    }
    try {
      const response = await api.interrupt(sessionID);
      if (!response.data?.interrupted && prior) {
        store.setState((state) => ({
          ...state,
          active: { ...state.active, [sessionID]: prior },
        }));
      }
      return response.data;
    } catch (error) {
      if (prior) {
        store.setState((state) => ({
          ...state,
          active: { ...state.active, [sessionID]: prior },
        }));
      }
      throw error;
    }
  }

  interruptSelectedSession() {
    const state = store.getState();
    const sessionID = state.ui.selectedSessionID;
    const runtime = sessionID ? state.active[sessionID] : null;
    if (!sessionID || !runtime?.turnID || runtime.state === "stopping") return false;
    void this.interrupt(sessionID).catch((error) => this.notice(errorMessage(error)));
    return true;
  }

  async compact(sessionID) {
    const previous = store.getState().active[sessionID] || null;
    const startedAt = new Date().toISOString();
    store.setState((state) => ({
      ...state,
      active: {
        ...state.active,
        [sessionID]: {
          state: "running",
          turnID: null,
          startedAt,
          activity: "Compacting",
        },
      },
    }));
    try {
      return await api.compact(sessionID);
    } catch (error) {
      store.setState((state) => {
        const current = state.active[sessionID];
        if (current?.startedAt !== startedAt || current?.activity !== "Compacting") return state;
        const active = { ...state.active };
        if (previous) active[sessionID] = previous;
        else delete active[sessionID];
        return { ...state, active };
      });
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
    this.queueMutationGeneration.set(sessionID, generation);
    const pending = this.queuePendingMutations.get(sessionID) || [];
    if (!this.queueServerRevisions.has(sessionID)) {
      this.queueServerRevisions.set(
        sessionID,
        Math.max(0, (queue.revision || 0) - pending.length),
      );
    }
    const mutation = { generation, operations };
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
      const expectedRevision = this.queueServerRevisions.get(sessionID) || 0;
      try {
        const response = await api.patchQueue(sessionID, { expectedRevision, operations });
        const remaining = (this.queuePendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        this.queuePendingMutations.set(sessionID, remaining);
        this.installAuthoritativeQueue(sessionID, response.data);
        return response.data;
      } catch (error) {
        const remaining = (this.queuePendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        this.queuePendingMutations.set(sessionID, remaining);
        try {
          const refreshed = await api.queue(sessionID);
          this.installAuthoritativeQueue(sessionID, refreshed.data);
        } catch {
          // A later SSE/resync will reconcile if the recovery read also fails.
        }
        if (error instanceof ApiError && error.code === "queue_revision_conflict") {
          this.notice("Queue changed elsewhere. Refreshed the current order.");
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

  async patchSession(sessionID, patch) {
    const before = store.getState();
    const previous = before.sessions[sessionID] || null;
    const activeIndex = before.sessionOrder.indexOf(sessionID);
    const archivedIndex = before.archivedOrder.indexOf(sessionID);
    const generation = (this.optimisticSessionGeneration.get(sessionID) || 0) + 1;
    this.optimisticSessionGeneration.set(sessionID, generation);
    const mutation = { generation, patch, previous, activeIndex, archivedIndex };
    this.sessionPendingMutations.set(
      sessionID,
      [...(this.sessionPendingMutations.get(sessionID) || []), mutation],
    );
    if (previous) {
      const optimistic = optimisticSessionPatch(previous, patch);
      store.setState((state) => installSessionSummary(state, optimistic, { moveToFront: true }));
    }

    const prior = this.sessionMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      try {
        const response = await api.patchSession(sessionID, patch);
        const remaining = (this.sessionPendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        this.sessionPendingMutations.set(sessionID, remaining);
        let visible = response.data;
        for (const item of remaining) visible = optimisticSessionPatch(visible, item.patch);
        const pendingSelection = this.pendingSelections.get(sessionID);
        if (pendingSelection) visible = { ...visible, selection: pendingSelection };
        store.setState((state) => installSessionSummary(state, visible, { moveToFront: true }));
        return response.data;
      } catch (error) {
        const remaining = (this.sessionPendingMutations.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        this.sessionPendingMutations.set(sessionID, remaining);
        try {
          const response = await api.getSession(sessionID);
          let visible = response.data;
          for (const item of remaining) visible = optimisticSessionPatch(visible, item.patch);
          const pendingSelection = this.pendingSelections.get(sessionID);
          if (pendingSelection) visible = { ...visible, selection: pendingSelection };
          store.setState((state) => installSessionSummary(state, visible, { moveToFront: Boolean(remaining.length) }));
        } catch {
          if (previous) {
            let visible = previous;
            for (const item of remaining) visible = optimisticSessionPatch(visible, item.patch);
            store.setState((state) => remaining.length
              ? installSessionSummary(state, visible, { moveToFront: true })
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
    const response = await api.regenerateTitle(sessionID);
    store.setState((state) => mergeSessionSummary(state, response.data));
    await this.refreshSessionLists();
    return response.data;
  }

  async forkSession(sessionID) {
    this.pendingNotice("Forking session…");
    try {
      const response = await api.forkSession(sessionID, {});
      store.setState((state) => installSessionSummary(state, response.data, { moveToFront: true }));
      this.selectSession(response.data.id);
      await this.loadSession(response.data.id, { force: true });
      void this.refreshSessionLists().catch(() => {});
    } catch (error) {
      this.clearNotice();
      throw error;
    }
  }

  async replyPermission(sessionID, requestID, reply, message = null) {
    this.resolveHumanInputOptimistically(sessionID, "permissions", requestID);
    try {
      await api.replyPermission(sessionID, requestID, { reply, message: message || null });
    } catch (error) {
      await this.refreshHumanInput(sessionID).catch(() => {});
      throw error;
    }
  }

  async replyQuestion(sessionID, requestID, answers) {
    this.resolveHumanInputOptimistically(sessionID, "questions", requestID);
    try {
      await api.replyQuestion(sessionID, requestID, answers);
    } catch (error) {
      await this.refreshHumanInput(sessionID).catch(() => {});
      throw error;
    }
  }

  async rejectQuestion(sessionID, requestID) {
    this.resolveHumanInputOptimistically(sessionID, "questions", requestID);
    try {
      await api.rejectQuestion(sessionID, requestID);
    } catch (error) {
      await this.refreshHumanInput(sessionID).catch(() => {});
      throw error;
    }
  }

  resolveHumanInputOptimistically(sessionID, field, requestID) {
    this.humanInputGeneration.set(
      sessionID,
      (this.humanInputGeneration.get(sessionID) || 0) + 1,
    );
    const data = store.getState().sessionData[sessionID] || {};
    const permissions = field === "permissions"
      ? (data.permissions || []).filter((item) => item.id !== requestID)
      : data.permissions || [];
    const questions = field === "questions"
      ? (data.questions || []).filter((item) => item.id !== requestID)
      : data.questions || [];
    this.installHumanInput(sessionID, permissions, questions);
  }

  async toggleTool(sessionID, toolName, enabled) {
    const current = store.getState().sessionData[sessionID]?.tools || [];
    const key = `${sessionID}:${toolName}`;
    const generation = (this.toolMutationGeneration.get(key) || 0) + 1;
    this.toolMutationGeneration.set(key, generation);
    if (current.length) {
      this.setSessionField(
        sessionID,
        "tools",
        current.map((tool) => tool.name === toolName ? { ...tool, enabled } : tool),
      );
    }
    const prior = this.toolMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      try {
        const response = await api.patchTools(
          sessionID,
          enabled ? { enabled: [toolName], disabled: [] } : { enabled: [], disabled: [toolName] },
        );
        if (
          this.toolMutationGeneration.get(key) === generation &&
          Array.isArray(response.data?.enabled)
        ) {
          const effective = new Set(response.data.enabled);
          const tools = store.getState().sessionData[sessionID]?.tools || [];
          this.setSessionField(
            sessionID,
            "tools",
            tools.map((tool) => tool.name === toolName
              ? { ...tool, enabled: effective.has(toolName) }
              : tool),
          );
        }
        return response.data;
      } catch (error) {
        if (this.toolMutationGeneration.get(key) === generation) {
          await this.refreshTools(sessionID).catch(() => {});
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.toolMutationChains.get(sessionID) === chained) {
        this.toolMutationChains.delete(sessionID);
      }
    });
    this.toolMutationChains.set(sessionID, chained);
    return chained;
  }

  async activateSkill(sessionID, skillName, prompt = null) {
    const skills = store.getState().sessionData[sessionID]?.skills;
    const candidate = skills?.available?.find((skill) => skill.name === skillName) || null;
    if (skills && candidate && !(skills.active || []).some((skill) => skill.name === skillName)) {
      this.setSessionField(sessionID, "skills", {
        ...skills,
        active: [...(skills.active || []), { ...candidate, active: true }],
      });
    }
    try {
      const response = await api.activateSkill(sessionID, skillName, prompt);
      if (skills && response.data?.activated) {
        const current = store.getState().sessionData[sessionID]?.skills || skills;
        this.setSessionField(sessionID, "skills", {
          ...current,
          active: [
            ...(current.active || []).filter((skill) => skill.name !== skillName),
            response.data.activated,
          ],
        });
      }
      return response.data;
    } catch (error) {
      await this.refreshSkills(sessionID).catch(() => {});
      throw error;
    }
  }

  async patchMcp(sessionID, serverName, patch) {
    const current = store.getState().sessionData[sessionID]?.mcp || [];
    const key = `${sessionID}:${serverName}`;
    const generation = (this.mcpMutationGeneration.get(key) || 0) + 1;
    this.mcpMutationGeneration.set(key, generation);
    if (current.length) {
      this.setSessionField(
        sessionID,
        "mcp",
        current.map((server) => server.name === serverName
          ? {
              ...server,
              ...(Object.prototype.hasOwnProperty.call(patch, "enabled") ? { enabled: patch.enabled } : {}),
              ...(Array.isArray(patch.enabledTools) ? { enabledTools: patch.enabledTools } : {}),
              ...(Array.isArray(patch.disabledTools) ? { disabledTools: patch.disabledTools } : {}),
            }
          : server),
      );
    }
    const prior = this.mcpMutationChains.get(key) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      try {
        const response = await api.patchMcp(sessionID, serverName, patch);
        if (this.mcpMutationGeneration.get(key) === generation) {
          this.schedule(`mcp:${sessionID}`, SUMMARY_REFRESH_MS, () => this.refreshMcp(sessionID));
        }
        return response.data;
      } catch (error) {
        if (this.mcpMutationGeneration.get(key) === generation) {
          await this.refreshMcp(sessionID).catch(() => {});
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.mcpMutationChains.get(key) === chained) {
        this.mcpMutationChains.delete(key);
      }
    });
    this.mcpMutationChains.set(key, chained);
    return chained;
  }

  async setSelection(sessionID, provider, model, reasoningEffort) {
    const previous = store.getState().sessions[sessionID]?.selection || null;
    const previousContextUsage = store.getState().sessionData[sessionID]?.contextUsage || null;
    const generation = (this.selectionGeneration.get(sessionID) || 0) + 1;
    this.selectionGeneration.set(sessionID, generation);
    const desired = {
      provider,
      model,
      reasoningEffort: reasoningEffort || null,
    };
    this.pendingSelections.set(sessionID, desired);
    store.setState((state) => {
      const session = state.sessions[sessionID];
      if (!session) return state;
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [sessionID]: {
            ...session,
            selection: desired,
            contextUsage: null,
          },
        },
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...state.sessionData[sessionID],
            contextUsage: null,
          },
        },
      };
    });

    const prior = this.selectionMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      try {
        const response = await api.selectModel(sessionID, desired);
        if (this.selectionGeneration.get(sessionID) === generation) {
          this.pendingSelections.delete(sessionID);
          const effective = response.data?.effective;
          if (effective) {
            store.setState((state) => {
              const session = state.sessions[sessionID];
              if (!session) return state;
              return {
                ...state,
                sessions: {
                  ...state.sessions,
                  [sessionID]: { ...session, selection: effective, contextUsage: null },
                },
              };
            });
          }
        }
        return response.data;
      } catch (error) {
        if (this.selectionGeneration.get(sessionID) === generation) {
          this.pendingSelections.delete(sessionID);
          if (previous) {
            store.setState((state) => {
              const session = state.sessions[sessionID];
              if (!session) return state;
              return {
                ...state,
                sessions: {
                  ...state.sessions,
                  [sessionID]: { ...session, selection: previous, contextUsage: previousContextUsage },
                },
                sessionData: {
                  ...state.sessionData,
                  [sessionID]: {
                    ...state.sessionData[sessionID],
                    contextUsage: previousContextUsage,
                  },
                },
              };
            });
          }
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.selectionMutationChains.get(sessionID) === chained) {
        this.selectionMutationChains.delete(sessionID);
      }
    });
    this.selectionMutationChains.set(sessionID, chained);
    return chained;
  }

  async cycleReasoningEffort() {
    const state = store.getState();
    const route = currentRoute();
    let directory = "";
    let provider = "";
    let model = "";
    let currentEffort = "";
    let sessionID = null;
    let draftID = null;
    if (route.name === "session") {
      sessionID = route.sessionID;
      const session = state.sessions[sessionID];
      if (!session) return false;
      const runtime = state.active[sessionID];
      if (runtime?.state && runtime.state !== "idle" && runtime.state !== "error") {
        this.notice("Thinking effort can be changed when the current turn settles.");
        return false;
      }
      directory = session.location?.directory || "";
      provider = session.selection?.provider || "";
      model = session.selection?.model || "";
      currentEffort = session.selection?.reasoningEffort || "";
    } else if (route.name === "new") {
      draftID = route.draftID;
      const draft = state.drafts[draftID];
      if (!draft) return false;
      directory = draft.location || "";
      provider = draft.provider || "";
      model = draft.model || "";
      currentEffort = draft.reasoningEffort || "";
    } else {
      return false;
    }
    if (!directory || !provider || !model) return false;
    const modelKey = `${directory}:${provider}:`;
    const models = state.models[modelKey]?.length
      ? state.models[modelKey]
      : await this.loadModels(directory, provider);
    const info = models.find((item) => item.id === model);
    const values = (info?.reasoningEfforts || []).filter(Boolean);
    if (!values.length) return false;
    const normalized = String(currentEffort || "high").toLowerCase();
    let index = values.findIndex((value) => String(value).toLowerCase() === normalized);
    if (index < 0) index = values.findIndex((value) => String(value).toLowerCase() === "high");
    const next = values[(index + 1 + values.length) % values.length];
    if (draftID) this.updateDraft(draftID, { reasoningEffort: next });
    if (sessionID) await this.setSelection(sessionID, provider, model, next);
    this.notice(`Thinking effort: ${next}`);
    return true;
  }

  focusQueueEditor() {
    const queue = document.querySelector(".queue-editor");
    if (!queue) {
      this.notice("No queued messages.");
      return false;
    }
    queue.scrollIntoView({ block: "nearest" });
    queue.querySelector("textarea, button, select")?.focus();
    return true;
  }

  focusModelSelector() {
    const model = document.querySelector('.selection-controls select[aria-label="Model"]');
    if (!model || model.disabled) {
      this.notice("Model selection is available when the session is idle.");
      return false;
    }
    model.focus();
    return true;
  }

  showShortcutHelp() {
    this.notice("Enter send/steer · Tab queue · ⇧Tab effort · Esc Esc stop · ⇧Enter/Ctrl+J/Esc Enter newline · Ctrl+U remove image · Ctrl+X O tools · Ctrl+X Ctrl+P processes · Ctrl+X Q queue · Ctrl+X M model · Ctrl+X T tree");
    return true;
  }

  async openInspector(mode, payload = {}) {
    this.clearNotice();
    const sessionID = store.getState().ui.selectedSessionID;
    if (!sessionID) return;
    store.setState((state) => ({ ...state, ui: { ...state.ui, inspector: { mode, ...payload } } }));
    if (mode === "tree") await this.refreshTree(sessionID);
    if (mode === "process") await this.refreshProcesses(sessionID);
    if (mode === "tool" && !payload.callID) await this.listToolCalls(sessionID);
    if (mode === "tools") await this.refreshTools(sessionID);
    if (mode === "skills") await this.refreshSkills(sessionID);
    if (mode === "mcp") await this.refreshMcp(sessionID);
    if (mode === "context") {
      const response = await api.context(sessionID);
      this.setSessionField(sessionID, "context", response.data);
    }
    if (mode === "tool" && payload.callID) await this.loadToolCall(sessionID, payload.callID);
    if (mode === "file" && payload.path) await this.loadFile(sessionID, payload.path);
  }

  closeInspector() {
    store.setState((state) => ({ ...state, ui: { ...state.ui, inspector: null } }));
  }

  async loadToolCall(sessionID, callID) {
    const [detail, output] = await Promise.all([api.toolCall(sessionID, callID), api.toolOutput(sessionID, callID)]);
    this.setSessionField(sessionID, "toolDetail", { ...detail.data, outputChunks: output.data, outputCursor: output.cursor });
  }

  async loadProcess(processID) {
    const [detail, output] = await Promise.all([api.process(processID), api.processOutput(processID)]);
    const sessionID = detail.data.sessionID;
    if (!sessionID) return;
    this.setSessionField(sessionID, "processDetail", { ...detail.data, outputChunks: output.data, outputCursor: output.cursor });
  }

  async loadFile(sessionID, path) {
    const session = store.getState().sessions[sessionID];
    if (!session) return;
    const content = await api.fsRead(session.location.directory, path);
    this.setSessionField(sessionID, "fileDetail", { path, content });
  }

  async treePreview(sessionID, targetID) {
    const response = await api.treePreview(sessionID, targetID);
    this.setSessionField(sessionID, "treePreview", response.data);
    return response.data;
  }

  async navigateTree(sessionID, targetID, branchSummary = null) {
    const tree = store.getState().sessionData[sessionID]?.tree;
    if (!tree) return;
    try {
      const response = await api.navigateTree(sessionID, { expectedRevision: tree.revision, targetID, branchSummary });
      await Promise.all([this.refreshTree(sessionID), this.refreshMessages(sessionID)]);
      if (response.data.editorText) this.setSessionField(sessionID, "editorHandoff", response.data.editorText);
      return response.data;
    } catch (error) {
      if (error instanceof ApiError && error.code === "tree_revision_conflict") {
        await this.refreshTree(sessionID);
        this.notice("Session tree changed elsewhere. Refreshed before navigation.");
        return null;
      }
      throw error;
    }
  }

  clearEditorHandoff(sessionID) {
    this.setSessionField(sessionID, "editorHandoff", null);
  }

  async labelTreeEntry(sessionID, entryID, label) {
    const tree = store.getState().sessionData[sessionID]?.tree;
    if (!tree) return;
    const normalized = String(label || "").trim().replace(/\s+/g, " ") || null;
    const generation = (this.treeMutationGeneration.get(sessionID) || 0) + 1;
    this.treeMutationGeneration.set(sessionID, generation);
    const mutation = { generation, entryID, label: normalized };
    const pending = [...(this.treePendingLabels.get(sessionID) || []), mutation];
    this.treePendingLabels.set(sessionID, pending);
    if (!this.treeServerRevisions.has(sessionID)) {
      this.treeServerRevisions.set(sessionID, tree.revision || 0);
    }
    this.setSessionField(sessionID, "tree", applyPendingTreeLabels(tree, pending));

    const prior = this.treeMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      const expectedRevision = this.treeServerRevisions.get(sessionID) || 0;
      try {
        const response = await api.patchTreeEntry(sessionID, entryID, {
          expectedRevision,
          label: normalized,
        });
        this.treeServerRevisions.set(sessionID, response.data.revision || 0);
        const remaining = (this.treePendingLabels.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        this.treePendingLabels.set(sessionID, remaining);
        const current = store.getState().sessionData[sessionID]?.tree;
        if (current) {
          const authoritative = mergeTreeEntry(current, response.data.entry, response.data.revision);
          this.setSessionField(
            sessionID,
            "tree",
            applyPendingTreeLabels(authoritative, remaining),
          );
        }
        return response.data;
      } catch (error) {
        const remaining = (this.treePendingLabels.get(sessionID) || [])
          .filter((item) => item.generation !== generation);
        this.treePendingLabels.set(sessionID, remaining);
        try {
          const refreshed = await api.tree(sessionID);
          this.treeServerRevisions.set(sessionID, refreshed.data.revision || 0);
          this.setSessionField(
            sessionID,
            "tree",
            applyPendingTreeLabels(refreshed.data, remaining),
          );
        } catch {
          // SSE or the next tree refresh will reconcile if recovery also fails.
        }
        if (error instanceof ApiError && error.code === "tree_revision_conflict") {
          this.notice("Session tree changed elsewhere. Refreshed before labeling.");
          return null;
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.treeMutationChains.get(sessionID) === chained) {
        this.treeMutationChains.delete(sessionID);
      }
    });
    this.treeMutationChains.set(sessionID, chained);
    return chained;
  }

  async listToolCalls(sessionID) {
    const response = await api.toolCalls(sessionID, { limit: 100 });
    this.setSessionField(sessionID, "toolCalls", response.data);
  }

  async signalProcess(processID, signal) {
    const response = await api.processSignal(processID, signal);
    const sessionID = response.data.sessionID;
    if (sessionID) {
      await this.refreshProcesses(sessionID);
      await this.loadProcess(processID);
    }
  }

  async sendProcessInput(processID, text) {
    const response = await api.processStdin(processID, text);
    const sessionID = response.data.sessionID;
    if (sessionID) {
      await this.refreshProcesses(sessionID);
      await this.loadProcess(processID);
    }
  }

  switchSession(delta) {
    const state = store.getState();
    const ids = state.sessionOrder;
    if (!ids.length) return;
    const current = ids.indexOf(state.ui.selectedSessionID);
    const next = ids[(Math.max(current, 0) + delta + ids.length) % ids.length];
    this.selectSession(next);
  }

  toggleSidebar() {
    store.setState((state) => ({ ...state, ui: { ...state.ui, sidebarOpen: !state.ui.sidebarOpen } }));
  }

  togglePalette(open = !store.getState().ui.commandPaletteOpen) {
    if (open) this.clearNotice();
    store.setState((state) => ({ ...state, ui: { ...state.ui, commandPaletteOpen: open } }));
  }

  async runPaletteCommand(command) {
    const state = store.getState();
    const sessionID = state.ui.selectedSessionID;
    this.togglePalette(false);
    if (command.action === "session.create") return this.createDraft();
    if (command.action === "command.list") return this.showShortcutHelp();
    if (!sessionID) return this.notice("Select a session first.");
    const session = state.sessions[sessionID];
    if (command.name === "pin") return this.patchSession(sessionID, { pinned: !session?.pinned });
    if (command.name === "title") {
      const title = window.prompt("Session title", session?.title || "");
      if (title !== null) return this.patchSession(sessionID, { title });
      return;
    }
    if (command.action === "session.title.regenerate") {
      this.pendingNotice("Regenerating title…");
      try {
        const updated = await this.regenerateTitle(sessionID);
        return this.notice(`Title updated to “${updated.title}”.`);
      } catch (error) {
        return this.notice(error?.message || String(error));
      }
    }
    if (command.action === "session.compact") return this.compact(sessionID);
    if (command.action === "session.get") return this.openInspector("context");
    if (command.action === "session.fork") return this.forkSession(sessionID);
    if (command.action === "session.tree") return this.openInspector("tree");
    if (command.action === "session.tool") return this.openInspector("tools");
    if (command.action === "session.mcp") return this.openInspector("mcp");
    if (command.action === "process.list") return this.openInspector("process");
    if (command.action === "session.skill") return this.openInspector("skills");
    if (command.action === "session.queue") return this.focusQueueEditor();
    if (command.action === "session.selection") return this.focusModelSelector();
    if (command.action === "upload.create") return this.notice("Paste or drop an image into the composer, or use the Image button.");
  }

  escape() {
    const state = store.getState();
    if (state.ui.commandPaletteOpen) {
      this.togglePalette(false);
      return true;
    }
    if (state.ui.inspector) {
      this.closeInspector();
      return true;
    }
    if (window.innerWidth <= 760 && state.ui.sidebarOpen) {
      this.toggleSidebar();
      return true;
    }
    return false;
  }

  notice(message) {
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: message, noticePending: false } }));
    this.schedule("notice", 3200, () => {
      store.setState((state) => ({ ...state, ui: { ...state.ui, notice: null, noticePending: false } }));
    });
  }

  pendingNotice(message) {
    clearTimeout(this.refreshTimers.get("notice"));
    this.refreshTimers.delete("notice");
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: message, noticePending: true } }));
  }

  clearNotice() {
    clearTimeout(this.refreshTimers.get("notice"));
    this.refreshTimers.delete("notice");
    if (!store.getState().ui.notice) return;
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: null, noticePending: false } }));
  }

  fail(error) {
    store.setState((state) => ({
      ...state,
      connection: { ...state.connection, status: "error", current: false, error: errorMessage(error) },
    }));
  }

  persistDone() {
    writeDone(store.getState().ui.doneUnreviewed);
  }
}

function promptAttachments(items) {
  return items.map((item) => ({ type: "file", uri: item.uri, name: item.name, mime: item.mime }));
}

function messagesContainPrompt(messages, livePrompt) {
  if (!livePrompt) return false;
  if (livePrompt.id) return messages.some((message) => message.inputID === livePrompt.id);
  const prompt = livePrompt.prompt || {};
  const expectedText = String(prompt.text || "");
  const expectedAttachments = (prompt.attachments || [])
    .map((item) => item.name || "")
    .sort();
  const admittedAt = Date.parse(livePrompt.timeCreated || "");
  return messages.some((message) => {
    if (message.type !== "user") return false;
    const createdAt = Date.parse(message.timeCreated || "");
    if (
      Number.isFinite(admittedAt) &&
      Number.isFinite(createdAt) &&
      createdAt < admittedAt - 2000
    ) return false;
    const text = (message.content || [])
      .filter((part) => part.type === "text")
      .map((part) => part.text || "")
      .join("\n");
    const attachments = (message.content || [])
      .filter((part) => part.type === "image")
      .map((part) => part.name || "")
      .sort();
    return (
      text === expectedText &&
      JSON.stringify(attachments) === JSON.stringify(expectedAttachments)
    );
  });
}

function reconcileLivePrompt(livePrompt, messages) {
  return livePrompt && !messagesContainPrompt(messages, livePrompt) ? livePrompt : null;
}

function liveEventCoalesceKey(event) {
  return `${event.sessionID || "global"}:${event.type}`;
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

function provisionalSessionTitle(text) {
  const title = String(text || "").trim().replace(/\s+/g, " ").slice(0, 72);
  return title || "New session";
}

function optimisticSessionPatch(session, patch) {
  const now = new Date().toISOString();
  const has = (key) => Object.prototype.hasOwnProperty.call(patch, key);
  return {
    ...session,
    title: has("title") ? patch.title : session.title,
    pinned: has("pinned") ? Boolean(patch.pinned) : session.pinned,
    archivedAt: has("archived")
      ? patch.archived ? now : null
      : session.archivedAt,
    time: { ...(session.time || {}), updated: now },
  };
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

function installSessionSummary(state, session, { moveToFront = false } = {}) {
  const sessionID = session.id;
  let sessionOrder = state.sessionOrder.filter((id) => id !== sessionID);
  let archivedOrder = state.archivedOrder.filter((id) => id !== sessionID);
  const target = session.archivedAt ? archivedOrder : sessionOrder;
  if (moveToFront || !target.includes(sessionID)) target.unshift(sessionID);
  return {
    ...state,
    sessions: { ...state.sessions, [sessionID]: session },
    sessionOrder,
    archivedOrder,
  };
}

function restoreSessionSummary(state, session, activeIndex, archivedIndex) {
  const sessionID = session.id;
  const sessionOrder = state.sessionOrder.filter((id) => id !== sessionID);
  const archivedOrder = state.archivedOrder.filter((id) => id !== sessionID);
  if (activeIndex >= 0) sessionOrder.splice(Math.min(activeIndex, sessionOrder.length), 0, sessionID);
  if (archivedIndex >= 0) archivedOrder.splice(Math.min(archivedIndex, archivedOrder.length), 0, sessionID);
  return {
    ...state,
    sessions: { ...state.sessions, [sessionID]: session },
    sessionOrder,
    archivedOrder,
  };
}

function applyQueueOperations(queue, operations) {
  let items = [...(queue?.items || [])];
  for (const operation of operations || []) {
    const index = items.findIndex((item) => item.id === operation.id);
    if (operation.op === "remove") {
      if (index >= 0) items.splice(index, 1);
      continue;
    }
    if (index < 0) continue;
    if (operation.op === "update") {
      items[index] = { ...items[index], prompt: operation.prompt };
      continue;
    }
    if (operation.op === "setDelivery") {
      items[index] = { ...items[index], delivery: operation.delivery };
      continue;
    }
    if (operation.op === "setPaused") {
      items[index] = { ...items[index], paused: operation.paused };
      continue;
    }
    const [item] = items.splice(index, 1);
    if (operation.op === "moveToStart") {
      items.unshift(item);
      continue;
    }
    if (operation.op === "moveBefore") {
      const targetID = operation.beforeID ?? operation.before_id;
      const target = items.findIndex((candidate) => candidate.id === targetID);
      items.splice(target >= 0 ? target : items.length, 0, item);
      continue;
    }
    if (operation.op === "moveAfter") {
      const targetID = operation.afterID ?? operation.after_id;
      const target = items.findIndex((candidate) => candidate.id === targetID);
      items.splice(target >= 0 ? target + 1 : items.length, 0, item);
      continue;
    }
    items.splice(Math.min(index, items.length), 0, item);
  }
  return { ...queue, items };
}

function queueSummary(queue) {
  const items = queue?.items || [];
  return {
    total: items.length,
    steering: items.filter((item) => item.delivery === "steer").length,
    queued: items.filter((item) => item.delivery === "queue").length,
    paused: items.filter((item) => item.paused).length,
    revision: queue?.revision || 0,
  };
}

function errorMessage(error) {
  if (error instanceof ApiError) return `${error.message}${error.requestID ? ` (${error.requestID})` : ""}`;
  return error?.message || String(error);
}

function parseSlashCommand(text) {
  const trimmed = String(text || "").trim();
  if (trimmed === "?") return { name: "?", args: "" };
  if (!trimmed.startsWith("/") || trimmed.includes("\n")) return null;
  const body = trimmed.slice(1);
  const separator = body.search(/\s/);
  if (separator < 0) return { name: body.toLowerCase(), args: "" };
  return {
    name: body.slice(0, separator).toLowerCase(),
    args: body.slice(separator).trim(),
  };
}

function splitFirstArgument(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return ["", ""];
  const separator = trimmed.search(/\s/);
  if (separator < 0) return [trimmed, ""];
  return [trimmed.slice(0, separator), trimmed.slice(separator).trim()];
}

function slashUsage(controller, usage) {
  controller.notice(`Usage: ${usage}`);
  return { handled: true, clear: false };
}

function idOr(value) { return value; }

export const controller = new AppController();
