import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";
import { YokeApi, api } from "../src/yoke/web/assets/js/api/client.js";
import { AgentRosterController } from "../src/yoke/web/assets/js/session/agents/controller.js";
import { agentPresentation, INACTIVE_AGENT_LIMIT, liveAgent, reconcileAgentRuns } from "../src/yoke/web/assets/js/session/agents/model.js";
import { reducePublicEvent } from "../src/yoke/web/assets/js/state/reducer.js";
import { store } from "../src/yoke/web/assets/js/state/store.js";

globalThis.fetch = () => { throw new Error("No real network in agent roster tests"); };
const dataURL = (source) => `data:text/javascript,${encodeURIComponent(source)}`;
const vendor = new URL("../src/yoke/web/assets/vendor/", import.meta.url);
register(dataURL(`export async function resolve(specifier, context, next) {
  if (specifier === "preact") return { url: ${JSON.stringify(new URL("preact.module.js", vendor).href)}, shortCircuit: true };
  if (specifier === "preact/hooks") return { url: ${JSON.stringify(new URL("hooks.module.js", vendor).href)}, shortCircuit: true };
  return next(specifier, context);
}`));

// Exercise the shipped Preact renderer, not a string template substitute. Any
// attempt to use HTML injection fails; display values must become text nodes.
class FakeNode {
  constructor(name, text = "") {
    this.nodeType = name === "#text" ? 3 : 1;
    this.localName = name;
    this.data = text;
    this.parentNode = null;
    this.childNodes = [];
    this.attributes = {};
    this.listeners = {};
    this.onclick = null;
    this.style = { setProperty() {} };
  }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    const siblings = this.parentNode?.childNodes || [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  get textContent() { return this.nodeType === 3 ? this.data : this.childNodes.map((child) => child.textContent).join(""); }
  set textContent(value) { this.childNodes = []; if (value) this.appendChild(new FakeNode("#text", String(value))); }
  set innerHTML(_) { throw new Error("Roster must not inject HTML"); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  removeEventListener(name) { delete this.listeners[name]; }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, before) {
    child.parentNode?.removeChild(child);
    const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
    this.childNodes.splice(index < 0 ? this.childNodes.length : index, 0, child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    this.childNodes.splice(this.childNodes.indexOf(child), 1);
    child.parentNode = null;
    return child;
  }
}
globalThis.document = {
  createElement: (name) => new FakeNode(name),
  createElementNS: (_, name) => new FakeNode(name),
  createTextNode: (text) => new FakeNode("#text", text),
};
const { h, render } = await import("../src/yoke/web/assets/vendor/htm-preact.js");
const { AgentRoster, AgentRosterView } = await import("../src/yoke/web/assets/js/session/agents/view.js");
const nodes = (node) => [node, ...node.childNodes.flatMap(nodes)];
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const run = (extra = {}) => ({
  schemaVersion: 1, agentId: "agent-a", runId: "run-a", sessionID: "s",
  parentRunId: null, parentAgentId: null, runtimeSessionID: 1,
  name: "Reviewer", provider: "provider", model: "model", status: "running", observation: "live",
  startedAt: "2026-09-28T10:00:00Z", finishedAt: null,
  lastSeenAt: "2026-09-28T10:00:00Z", updatedAt: "2026-09-28T10:00:00Z",
  version: 1, lastToolName: "read", errorType: null, ...extra,
});

function setup(t) {
  store.reset();
  store.setState((state) => ({
    ...state, connection: { ...state.connection, current: true, serverInstanceID: "server-a" },
    ui: { ...state.ui, selectedSessionID: "s" },
  }));
  const requests = [];
  t.mock.method(api, "agentRuns", (sessionID, options) => {
    const reply = deferred();
    requests.push({ ...reply, sessionID, signal: options.signal });
    return reply.promise;
  });
  const controller = new AgentRosterController({ api, store });
  t.after(() => controller.stop());
  return { controller, requests, start: async () => { controller.start(); await flush(); } };
}
const event = (sessionID = "s", type = "session.agent.updated", data = {}) =>
  store.setState((state) => reducePublicEvent(state, { sessionID, type, data }));
const select = (selectedSessionID) => store.setState((state) => ({ ...state, ui: { ...state.ui, selectedSessionID } }));
const connect = (current, extra = {}) => store.setState((state) => ({ ...state, connection: { ...state.connection, current, ...extra } }));
const draw = (state, retry = () => {}) => {
  const root = new FakeNode("main");
  render(h(AgentRosterView, { state, retry }), root);
  return root;
};

test("agent snapshots use the authenticated same-origin API and encode session ownership", async (t) => {
  const client = new YokeApi();
  client.setToken("test-token");
  const signal = new AbortController().signal;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, "/api/v1/agent-run?sessionID=s+%26+other%3Dx");
    assert.equal(options.headers.Authorization, "Bearer test-token");
    assert.equal(options.signal, signal);
    return { ok: true, status: 200, headers: new Headers({ "content-type": "application/json" }), json: async () => ({ data: [] }) };
  });
  assert.deepEqual(await client.agentRuns("s & other=x", { signal }), { data: [] });
});

test("initial selection loads agents and post-turn events update the mounted roster", async (t) => {
  const f = setup(t);
  const root = new FakeNode("main");
  render(h(AgentRoster), root);
  t.after(() => render(null, root));
  await flush();
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].sessionID, "s");
  f.requests[0].resolve({ data: [run()] });
  await flush();
  assert.match(root.textContent, /1 active/);
  assert.match(root.textContent, /Reviewer/);
  assert.match(root.textContent, /provider \/ model/);
  assert.match(root.textContent, /Last toolread/);
  event("s", "session.active.changed", { state: "idle" });
  event();
  await flush();
  assert.equal(f.requests.length, 2, "no foreground tool read needed after the turn ends");
  f.requests[1].resolve({ data: [run({ status: "completed", version: 2 })] });
  await flush();
  assert.match(root.textContent, /0 active/);
  assert.match(root.textContent, /Reviewer/);
  assert.match(root.textContent, /Completed/);
  assert.equal(nodes(root).filter((node) => node.localName === "li").length, 1);
});

test("late responses cannot cross session selection, including A to B to A", async (t) => {
  const f = setup(t);
  await f.start();
  select("other");
  await flush();
  assert.equal(f.requests[0].signal.aborted, true);
  assert.equal(f.controller.getState().rows.length, 0);
  select("s");
  await flush();
  f.requests[2].resolve({ data: [run({ name: "Current session" })] });
  f.requests[1].resolve({ data: [run({ sessionID: "other", name: "Late session" })] });
  f.requests[0].resolve({ data: [run({ name: "Old selection of this same session" })] });
  await flush();
  assert.equal(f.controller.getState().sessionID, "s");
  assert.equal(f.controller.getState().rows[0].name, "Current session");
});

test("newer refresh owns the result even if an aborted request finishes last", async (t) => {
  const f = setup(t);
  await f.start();
  const latest = f.controller.refresh();
  f.requests[1].resolve({ data: [run({ status: "failed", version: 3 })] });
  await latest;
  f.requests[0].resolve({ data: [run()] });
  await flush();
  assert.equal(f.controller.getState().rows[0].status, "failed");
  const older = f.controller.refresh();
  f.requests[2].resolve({ data: [run({ status: "completed", version: 2 })] });
  await older;
  assert.equal(f.controller.getState().rows[0].status, "failed");
});

test("duplicate revisions and heartbeat-only fields do not repaint; events coalesce", async (t) => {
  const f = setup(t);
  await f.start();
  f.requests[0].resolve({ data: [run()] });
  await flush();
  let changes = 0;
  f.controller.subscribe(() => changes++);
  for (let i = 0; i < 20; i++) event();
  await flush();
  assert.equal(f.requests.length, 2);
  f.requests[1].resolve({ data: [run({ lastSeenAt: "2026-09-28T10:01:00Z" })] });
  await flush();
  assert.equal(changes, 0);
  for (let i = 0; i < 100; i++) event("s", "session.message.updated", { content: String(i) });
  event("other");
  event("s", "session.active.changed", { state: "idle" });
  await flush();
  assert.equal(changes, 0);
  assert.equal(f.requests.length, 2, "no token-driven refresh or idle poll");
});

test("sustained invalidations let each snapshot finish and schedule one follow-up", async (t) => {
  const f = setup(t);
  await f.start();
  f.requests[0].resolve({ data: [run()] });
  await flush();
  event();
  await flush();
  assert.equal(f.requests.length, 2);
  // Separate event-loop turns model SSE events arriving while the HTTP response
  // is delayed. Unlike a synchronous burst, microtask coalescing alone cannot help.
  for (let i = 0; i < 8; i++) { event(); await flush(); }
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[1].signal.aborted, false);
  f.requests[1].resolve({ data: [run({ status: "completed", version: 2 })] });
  await flush();
  assert.equal(f.controller.getState().rows[0].status, "completed");
  assert.equal(f.requests.length, 3, "one fresh read follows all queued invalidations");
  for (let i = 0; i < 8; i++) { event(); await flush(); }
  assert.equal(f.requests.length, 3);
  assert.equal(f.controller.getState().rows.filter(liveAgent).length, 0);
  // Ownership changes must still cancel immediately, without waiting for a read.
  select("other");
  await flush();
  assert.equal(f.requests[2].signal.aborted, true);
  assert.equal(f.requests[3].sessionID, "other");
  f.requests[2].resolve({ data: [run()] });
  await flush();
  assert.equal(f.controller.getState().rows.length, 0);
});

test("latest run wins over late completion of an earlier activation", () => {
  const old = run({ status: "completed", version: 99 });
  const current = run({ runId: "run-b", version: 4, startedAt: "2026-09-28T10:02:00Z" });
  assert.deepEqual(reconcileAgentRuns([], [old, current, old], "s"), [current]);
  assert.deepEqual(reconcileAgentRuns([current], [old], "s"), [current]);
  assert.deepEqual(reconcileAgentRuns([old], [current], "s"), [current]);
});

test("sub-millisecond run starts retain identity ordering across late old revisions", () => {
  const old = run({ status: "completed", version: 5, startedAt: "2026-09-28T10:00:00.000100+00:00" });
  const current = run({ runId: "new-run", version: 4, startedAt: "2026-09-28T10:00:00.000900+00:00" });
  for (const inputs of [[old, current], [current, old]]) {
    assert.deepEqual(reconcileAgentRuns([], inputs, "s"), [current]);
    assert.deepEqual(reconcileAgentRuns([current], inputs, "s"), [current]);
  }
  assert.deepEqual(reconcileAgentRuns([current], [old], "s"), [current]);
  assert.equal(reconcileAgentRuns([], [old, current], "s").filter(liveAgent).length, 1);
});

test("disconnect and resync remove running claims; reconnect snapshots restore only live work", async (t) => {
  const f = setup(t);
  await f.start();
  f.requests[0].resolve({ data: [run()] });
  await flush();
  connect(false, { status: "resyncing" });
  const disconnected = draw(f.controller.getState());
  assert.match(disconnected.textContent, /Active count unavailable/);
  assert.match(disconnected.textContent, /Stale observation/);
  assert.doesNotMatch(disconnected.textContent, /Running|1 active/);
  connect(true);
  await flush();
  f.requests[1].resolve({ data: [run({ observation: "lost", version: 2 })] });
  await flush();
  assert.match(draw(f.controller.getState()).textContent, /0 active/);
  assert.match(draw(f.controller.getState()).textContent, /Observation lost/);
  connect(false);
  connect(true, { serverInstanceID: "server-b" });
  await flush();
  f.requests[2].resolve({ data: [run({ observation: "lost", version: 0 })] });
  await flush();
  assert.equal(f.controller.getState().rows[0].version, 0, "new daemon has a new revision scope");
  event();
  await flush();
  f.requests[3].resolve({ data: [run({ runId: "reactivated", version: 1, startedAt: "2026-09-28T11:00:00Z" })] });
  await flush();
  assert.match(draw(f.controller.getState()).textContent, /1 active/);
});

test("auth changes and unmount invalidate in-flight snapshots", async (t) => {
  const f = setup(t);
  await f.start();
  store.setState((state) => ({ ...state, auth: { ...state.auth, token: "other-token" } }));
  await flush();
  f.requests[0].resolve({ data: [run()] });
  await flush();
  assert.equal(f.controller.getState().rows.length, 0);
  f.controller.stop();
  f.requests[1].resolve({ data: [run()] });
  await flush();
  assert.equal(f.controller.getState().rows.length, 0);
  assert.equal(f.requests[1].signal.aborted, true);
});

test("initial reconnect waits for resync and late reads cannot restore disconnected running claims", async (t) => {
  const f = setup(t);
  connect(false);
  await f.start();
  assert.equal(f.requests.length, 0);
  connect(true);
  await flush();
  assert.equal(f.requests.length, 1);
  connect(false);
  f.requests[0].resolve({ data: [run()] });
  await flush();
  assert.equal(f.controller.getState().current, false);
  assert.equal(f.controller.getState().rows.length, 0);
  connect(true);
  await flush();
  f.requests[1].resolve({ data: [run()] });
  await flush();
  assert.match(draw(f.controller.getState()).textContent, /1 active/);
  store.setState((state) => ({ ...state, auth: { ...state.auth, required: true } }));
  assert.equal(f.controller.getState().sessionID, null);
  assert.equal(f.controller.getState().rows.length, 0);
});

test("read failure preserves rows without claiming active execution; retry recovers", async (t) => {
  const f = setup(t);
  await f.start();
  f.requests[0].resolve({ data: [run()] });
  await flush();
  const failure = f.controller.refresh();
  f.requests[1].reject(new Error("untrusted server detail"));
  await failure;
  const root = draw(f.controller.getState(), () => f.controller.refresh());
  assert.match(root.textContent, /Could not refresh agents/);
  assert.match(root.textContent, /Stale observation/);
  assert.doesNotMatch(root.textContent, /Running|Completed|untrusted server detail/);
  const button = nodes(root).find((node) => node.localName === "button");
  button.listeners.click.call(button, { type: "click" });
  f.requests[2].resolve({ data: [] });
  await flush();
  assert.equal(f.controller.getState().rows.length, 0, "a full empty snapshot removes historical claims");
  assert.match(draw(f.controller.getState()).textContent, /No agents registered/);
});

test("all terminal statuses stay distinct, lost and stale are never success or running", () => {
  const rows = ["running", "completed", "failed", "cancelled", "interrupted"].map((status) => run({ agentId: status, status }));
  rows.push(run({ agentId: "lost", observation: "lost" }), run({ agentId: "stale", observation: "stale" }));
  const root = draw({ sessionID: "s", current: true, rows });
  assert.match(root.textContent, /1 active/);
  for (const label of ["Running", "Completed", "Failed", "Cancelled", "Interrupted", "Observation lost", "Stale observation"]) assert.ok(root.textContent.includes(label));
  for (const observation of ["stale", "lost"]) {
    for (const status of ["running", "completed", "failed", "cancelled", "interrupted"]) {
      const presentation = agentPresentation(run({ observation, status }));
      assert.notEqual(presentation.tone, "completed");
      assert.notEqual(presentation.tone, "running");
      assert.doesNotMatch(presentation.label, /Running/);
    }
  }
  assert.ok(nodes(root).some((node) => node.localName === "summary"));
  assert.ok(nodes(root).some((node) => node.attributes["aria-live"] === "polite"));
  assert.ok(nodes(root).some((node) => node.attributes["aria-label"] === "Session agents"));
});

test("hostile display text remains literal text through the real Preact renderer", () => {
  const hostile = '<img src=x onerror="globalThis.pwned=true"> & <script>pwned()</script>';
  const row = run({ name: hostile, provider: hostile, model: hostile, lastToolName: hostile, errorType: hostile, status: "failed" });
  const root = draw({ sessionID: "s", current: true, rows: [row] });
  assert.equal(root.textContent.split(hostile).length - 1, 5);
  assert.equal(nodes(root).some((node) => ["img", "script"].includes(node.localName)), false);
  assert.equal(nodes(root).some((node) => Object.keys(node.attributes).some((name) => name.startsWith("on"))), false);
  assert.equal(globalThis.pwned, undefined);
});

test("bounded inactive history does not truncate live count or accept foreign and malformed records", () => {
  const live = Array.from({ length: 120 }, (_, i) => run({ agentId: `live-${i}` }));
  const completed = Array.from({ length: 300 }, (_, i) => run({ agentId: `done-${i}`, status: "completed", version: i }));
  const rows = reconcileAgentRuns([], [
    ...completed, ...live, run({ sessionID: "other" }), run({ schemaVersion: 2 }),
    run({ version: "1" }), run({ status: "success" }), run({ observation: "unknown" }), null,
  ], "s");
  assert.equal(rows.length, 120 + INACTIVE_AGENT_LIMIT);
  assert.equal(rows.filter(liveAgent).length, 120);
  assert.equal(new Set(rows.map((row) => row.agentId)).size, rows.length);
  assert.equal(rows.some((row) => row.agentId === "agent-a"), false);
});
