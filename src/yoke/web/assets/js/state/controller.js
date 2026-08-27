// @ts-check

import { ApiError, api } from "../api/client.js";
import { SseClient } from "../api/sse.js";
import { randomUUID } from "../lib/id.js";
import { currentRoute, draftPath, navigate, sessionPath } from "../router/router.js";
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

class AppController {
  constructor() {
    this.sse = null;
    this.bufferEvents = true;
    this.eventBuffer = [];
    this.bootstrapping = false;
    this.resyncing = false;
    this.refreshTimers = new Map();
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
      const [capabilities, active, commands, recent, current, archived, providers] = await Promise.all([
        api.capabilities(),
        api.activeSessions(),
        api.commands(),
        api.recentLocations(),
        api.listSessions({ archived: false, limit: 100 }),
        api.listSessions({ archived: true, limit: 30 }),
        api.providers(),
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
          providers: providers.data,
        };
        next = installActiveSnapshot(next, active.data);
        for (const event of this.eventBuffer) next = reducePublicEvent(next, event);
        return next;
      });
      this.bufferEvents = false;
      this.eventBuffer = [];
      await this.resolveVisibleLocations();
      await this.refreshProcessLocalState();
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
    if (!event.durable && ["session.message.updated", "session.compaction.delta", "session.context.updated"].includes(event.type)) {
      this.pendingLiveEvents.set(`${event.sessionID || "global"}:${event.type}`, event);
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
      return {
        ...state,
        sessions,
        sessionOrder: current.data.map((item) => item.id),
        archivedOrder: archived.data.map((item) => item.id),
        sessionsCursor: current.cursor?.next || null,
        archivedCursor: archived.cursor?.next || null,
      };
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
    store.setState((state) => mergeSessionSummary(state, response.data));
  }

  async resolveVisibleLocations() {
    const state = store.getState();
    const directories = [...new Set(
      [...state.sessionOrder, ...state.archivedOrder]
        .map((id) => state.sessions[id]?.location?.directory)
        .filter(Boolean),
    )];
    for (const directory of directories) {
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
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], loading: true } },
    }));
    try {
      const [session, messages, queue, permissions, questions] = await Promise.all([
        api.getSession(sessionID),
        api.messages(sessionID, { limit: 100, order: "desc" }),
        api.queue(sessionID),
        api.permissions(sessionID),
        api.questions(sessionID),
      ]);
      store.setState((state) => ({
        ...mergeSessionSummary(state, session.data),
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...state.sessionData[sessionID],
            loaded: true,
            loading: false,
            messages: [...messages.data].reverse(),
            messageCursor: messages.cursor?.next || null,
            queue: queue.data,
            permissions: permissions.data,
            questions: questions.data,
          },
        },
      }));
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
            messages: [...response.data].reverse().concat(state.sessionData[sessionID]?.messages || []),
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
    if (!store.getState().sessionData[sessionID]?.loaded) return;
    const response = await api.messages(sessionID, { limit: 100, order: "desc" });
    const messages = [...response.data].reverse();
    store.setState((state) => {
      const current = state.sessionData[sessionID] || {};
      const livePrompt = current.livePrompt;
      return {
        ...state,
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...current,
            messages,
            liveAssistant: null,
            livePrompt:
              current.lastError && livePrompt && messagesContainPrompt(messages, livePrompt)
                ? null
                : livePrompt || null,
            failedPrompts: (current.failedPrompts || []).filter(
              (prompt) => !messagesContainPrompt(messages, prompt),
            ),
          },
        },
      };
    });
  }

  async refreshQueue(sessionID) {
    if (!store.getState().sessionData[sessionID]?.loaded) return;
    const response = await api.queue(sessionID);
    this.installQueue(sessionID, response.data);
  }

  async refreshHumanInput(sessionID) {
    const [permissions, questions] = await Promise.all([api.permissions(sessionID), api.questions(sessionID)]);
    store.setState((state) => ({
      ...state,
      attention: {
        ...state.attention,
        [sessionID]: { permissions: permissions.data.length, questions: questions.data.length },
      },
      sessionData: {
        ...state.sessionData,
        [sessionID]: { ...state.sessionData[sessionID], permissions: permissions.data, questions: questions.data },
      },
    }));
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
    for (const id of ids) await Promise.allSettled([this.refreshHumanInput(id), this.refreshProcesses(id)]);
  }

  async refreshTree(sessionID) {
    if (!store.getState().sessionData[sessionID]?.tree && store.getState().ui.inspector?.mode !== "tree") return;
    const response = await api.tree(sessionID);
    this.setSessionField(sessionID, "tree", response.data);
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

  installQueue(sessionID, queue) {
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
    const location = draft.location || store.getState().recentLocations[0]?.directory;
    if (!location) throw new Error("Choose a working location first.");
    const sessionID = `web_${randomUUID()}`;
    const selection = draft.provider && draft.model ? {
      provider: draft.provider,
      model: draft.model,
      reasoningEffort: draft.reasoningEffort || null,
    } : undefined;
    await api.createSession({
      id: sessionID,
      location: { directory: location },
      title: provisionalSessionTitle(draft.text),
      selection,
    });
    await api.admitPrompt(sessionID, {
      id: `inp_${randomUUID()}`,
      prompt: { text: draft.text || "", attachments: promptAttachments(draft.attachments || []) },
      delivery,
      resume: true,
    });
    this.deleteDraft(id);
    await this.refreshSessionLists();
    this.selectSession(sessionID);
    await this.loadSession(sessionID, { force: true });
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
    await api.admitPrompt(sessionID, {
      id: `inp_${randomUUID()}`,
      prompt: { text, attachments: promptAttachments(attachments) },
      delivery,
      resume: true,
    });
    await this.refreshQueue(sessionID);
  }

  async interrupt(sessionID) {
    await api.interrupt(sessionID);
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
    return api.compact(sessionID);
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
    try {
      const response = await api.patchQueue(sessionID, { expectedRevision: queue.revision, operations });
      this.installQueue(sessionID, response.data);
    } catch (error) {
      if (error instanceof ApiError && error.code === "queue_revision_conflict") {
        await this.refreshQueue(sessionID);
        this.notice("Queue changed elsewhere. Refreshed the current order.");
        return;
      }
      throw error;
    }
  }

  async patchSession(sessionID, patch) {
    const response = await api.patchSession(sessionID, patch);
    store.setState((state) => mergeSessionSummary(state, response.data));
    await this.refreshSessionLists();
  }

  async regenerateTitle(sessionID) {
    const response = await api.regenerateTitle(sessionID);
    store.setState((state) => mergeSessionSummary(state, response.data));
    await this.refreshSessionLists();
    return response.data;
  }

  async forkSession(sessionID) {
    const response = await api.forkSession(sessionID, {});
    await this.refreshSessionLists();
    this.selectSession(response.data.id);
    await this.loadSession(response.data.id, { force: true });
  }

  async replyPermission(sessionID, requestID, reply, message = null) {
    await api.replyPermission(sessionID, requestID, { reply, message: message || null });
    await this.refreshHumanInput(sessionID);
  }

  async replyQuestion(sessionID, requestID, answers) {
    await api.replyQuestion(sessionID, requestID, answers);
    await this.refreshHumanInput(sessionID);
  }

  async rejectQuestion(sessionID, requestID) {
    await api.rejectQuestion(sessionID, requestID);
    await this.refreshHumanInput(sessionID);
  }

  async toggleTool(sessionID, toolName, enabled) {
    await api.patchTools(sessionID, enabled ? { enabled: [toolName], disabled: [] } : { enabled: [], disabled: [toolName] });
    await this.refreshTools(sessionID);
  }

  async activateSkill(sessionID, skillName, prompt = null) {
    await api.activateSkill(sessionID, skillName, prompt);
    await this.refreshSkills(sessionID);
  }

  async patchMcp(sessionID, serverName, patch) {
    await api.patchMcp(sessionID, serverName, patch);
    await this.refreshMcp(sessionID);
  }

  async setSelection(sessionID, provider, model, reasoningEffort) {
    await api.selectModel(sessionID, { provider, model, reasoningEffort: reasoningEffort || null });
    await this.refreshSessionSummary(sessionID);
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
    try {
      await api.patchTreeEntry(sessionID, entryID, { expectedRevision: tree.revision, label: label || null });
      await this.refreshTree(sessionID);
    } catch (error) {
      if (error instanceof ApiError && error.code === "tree_revision_conflict") {
        await this.refreshTree(sessionID);
        this.notice("Session tree changed elsewhere. Refreshed before labeling.");
        return;
      }
      throw error;
    }
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
    if (command.action === "command.list") return this.notice("Primary shortcuts: ⌘K palette, ⌘N new session, ⌘B sidebar, Esc close, ⌥↑/↓ switch sessions.");
    if (!sessionID) return this.notice("Select a session first.");
    const session = state.sessions[sessionID];
    if (command.name === "pin") return this.patchSession(sessionID, { pinned: !session?.pinned });
    if (command.name === "title") {
      const title = window.prompt("Session title", session?.title || "");
      if (title !== null) return this.patchSession(sessionID, { title });
      return;
    }
    if (command.action === "session.title.regenerate") {
      this.notice("Regenerating title…");
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
    if (command.action === "session.queue") return document.querySelector(".queue-editor")?.scrollIntoView({ block: "nearest" });
    if (command.action === "session.selection") return document.querySelector(".selection-controls select")?.focus();
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
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: message } }));
    this.schedule("notice", 3200, () => {
      store.setState((state) => ({ ...state, ui: { ...state.ui, notice: null } }));
    });
  }

  clearNotice() {
    clearTimeout(this.refreshTimers.get("notice"));
    this.refreshTimers.delete("notice");
    if (!store.getState().ui.notice) return;
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: null } }));
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

function provisionalSessionTitle(text) {
  const title = String(text || "").trim().replace(/\s+/g, " ").slice(0, 72);
  return title || "New session";
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

function idOr(value) { return value; }

export const controller = new AppController();
