import { reconcileAgentRuns } from "./model.js";

const emptyState = (sessionID = null) => ({
  sessionID, rows: [], loaded: false, loading: Boolean(sessionID), current: false, error: null,
});

// Own only the selected session. No retained session cache or idle polling.
export class AgentRosterController {
  constructor({ api, store }) {
    this.api = api;
    this.store = store;
    this.state = emptyState();
    this.listeners = new Set();
    this.context = null;
    this.generation = 0;
    this.abort = null;
    this.queued = null;
    this.pending = false;
    this.unsubscribe = null;
  }

  getState() { return this.state; }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  publish(patch) {
    if (Object.entries(patch).every(([key, value]) => this.state[key] === value)) return;
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }

  start() {
    if (this.unsubscribe) return;
    this.unsubscribe = this.store.subscribe(() => this.sync());
    this.sync();
  }

  stop() {
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.context = null;
    this.invalidate();
  }

  invalidate() {
    this.generation += 1;
    this.abort?.abort();
    this.abort = null;
    this.queued = null;
    this.pending = false;
  }

  sync() {
    const state = this.store.getState();
    const sessionID = state.auth.required || state.ui.newSession ? null : state.ui.selectedSessionID;
    const next = {
      sessionID, token: state.auth.token, instance: state.connection.serverInstanceID,
      connected: state.connection.current && !state.auth.required,
      revision: state.sessionData[sessionID]?.agentRunsRevision || 0,
    };
    const prior = this.context;
    this.context = next;
    if (!prior || prior.sessionID !== sessionID || prior.token !== next.token || prior.instance !== next.instance) {
      this.invalidate();
      this.publish(emptyState(sessionID));
      if (sessionID && next.connected) this.queueRefresh();
    } else if (prior.connected !== next.connected) {
      this.invalidate();
      this.publish({ current: false, error: null });
      if (sessionID && next.connected) this.queueRefresh();
    } else if (sessionID && next.connected && prior.revision !== next.revision) {
      this.queueRefresh();
    }
  }

  queueRefresh() {
    // A busy stream must not repeatedly abort the only response that could
    // update the roster. Keep one read in flight and coalesce its follow-up.
    if (this.abort) { this.pending = true; return; }
    if (this.queued) return;
    const ticket = {};
    this.queued = ticket;
    queueMicrotask(() => {
      if (this.queued !== ticket) return;
      this.queued = null;
      void this.refresh();
    });
  }

  async refresh() {
    const { sessionID, connected } = this.context || {};
    if (!sessionID || !connected || !this.unsubscribe) return;
    this.invalidate();
    const generation = this.generation;
    const abort = new AbortController();
    this.abort = abort;
    if (!this.state.current) this.publish({ loading: true, error: null });
    try {
      const response = await this.api.agentRuns(sessionID, { signal: abort.signal });
      if (generation !== this.generation) return;
      if (!Array.isArray(response?.data)) throw new Error("Invalid agent snapshot");
      this.publish({
        rows: reconcileAgentRuns(this.state.rows, response.data, sessionID),
        loaded: true, loading: false, current: true, error: null,
      });
    } catch {
      if (generation !== this.generation) return;
      // A failed read is not evidence of completion or continued execution.
      this.publish({ loading: false, current: false, error: "Could not refresh agents. Agent state is not current." });
    } finally {
      if (generation === this.generation) {
        this.abort = null;
        if (this.pending) {
          this.pending = false;
          this.queueRefresh();
        }
      }
    }
  }
}
