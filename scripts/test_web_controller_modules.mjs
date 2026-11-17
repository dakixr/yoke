import assert from "node:assert/strict";
import test from "node:test";
import { api } from "../src/yoke/web/assets/js/api/client.js";
import { ConnectionController } from "../src/yoke/web/assets/js/state/connection.js";
import { CommandController } from "../src/yoke/web/assets/js/state/commands.js";
import { ConfigurationController } from "../src/yoke/web/assets/js/state/configuration.js";
import { HumanInputController } from "../src/yoke/web/assets/js/state/human-input.js";
import { BrowserLifecycle } from "../src/yoke/web/assets/js/state/lifecycle.js";
import { InputController } from "../src/yoke/web/assets/js/state/inputs.js";
import { InspectorStateController } from "../src/yoke/web/assets/js/inspector/state/controller.js";
import { store } from "../src/yoke/web/assets/js/state/store.js";

function lifecycle() {
  return { epoch: 0, owns(epoch) { return this.epoch === epoch; }, scheduler: { timers: new Map() } };
}
function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}
function seed() {
  store.reset();
  store.setState((state) => ({ ...state,
    sessions: { s: { id: "s", title: "Old", pinned: false, selection: { provider: "p", model: "old" } } },
    sessionData: { s: { loaded: true } },
    ui: { ...state.ui, selectedSessionID: "s" },
  }));
}
test.beforeEach(seed);

test("connection consumes URL credentials without dropping unrelated query/fragment fields", (t) => {
  const values = new Map();
  t.mock.method(api, "setToken", () => {});
  globalThis.window = { location: { href: "http://fixture/session/s?token=query&view=tree#token=fragment&other=kept" } };
  globalThis.sessionStorage = { setItem: (key, value) => values.set(key, value) };
  const replacements = [];
  globalThis.history = { replaceState: (...args) => replacements.push(args) };
  t.after(() => { delete globalThis.window; delete globalThis.sessionStorage; delete globalThis.history; });
  const connection = new ConnectionController({ lifecycle: lifecycle(), actions: {}, inputs: {} });
  connection.consumeURLToken();
  assert.equal(values.get("yoke.web.token"), "fragment");
  assert.deepEqual(replacements, [[{}, "", "/session/s?view=tree#other=kept"]]);
});

test("slash and palette dispatch retain arguments, usage results and notice state", async () => {
  const calls = [];
  const commands = new CommandController({ lifecycle: lifecycle(), actions: {
    schedule: (...args) => calls.push(["schedule", ...args]),
    patchSession: async (...args) => calls.push(["patch", ...args]),
    openInspector: async (...args) => calls.push(["inspect", ...args]),
  } });
  assert.deepEqual(await commands.runSlashCommand("plain text"), { handled: false });
  assert.deepEqual(await commands.runSlashCommand("/title New title", { sessionID: "s" }), { handled: true });
  assert.deepEqual(calls.shift(), ["patch", "s", { title: "New title" }]);
  await commands.runSlashCommand("/mcp server extra", { sessionID: "s" });
  assert.deepEqual(calls.shift(), ["inspect", "mcp", { serverName: "server" }]);
  assert.deepEqual(await commands.runSlashCommand("/pin extra", { sessionID: "s" }), { handled: true, clear: false });
  assert.equal(store.getState().ui.notice, "Usage: /pin");
  const [key, delay, task] = calls.shift().slice(1);
  assert.equal(key, "notice");
  assert.equal(delay, 3200);
  task();
  assert.equal(store.getState().ui.notice, null);
  await commands.runPaletteCommand({ name: "pin" });
  assert.deepEqual(calls.shift(), ["patch", "s", { pinned: true }]);
});

test("queue snapshot caller ownership cannot install a retired transcript load", async (t) => {
  const gate = deferred();
  t.mock.method(api, "queue", () => gate.promise);
  const inputs = new InputController({ lifecycle: lifecycle(), workspace: {}, actions: {} });
  let owns = true;
  const loading = inputs.loadQueue("s", () => owns);
  owns = false;
  gate.resolve({ data: { revision: 8, items: [{ id: "old" }] } });
  await loading;
  assert.equal(store.getState().sessionData.s.queue, undefined);
  // Ignoring the stale load must also leave the revision fence untouched.
  inputs.installAuthoritativeQueue("s", { revision: 2, items: [] });
  assert.equal(store.getState().sessionData.s.queue.revision, 2);
});

test("human-input snapshot cannot restore a question answered during the read", async (t) => {
  const gate = deferred();
  const question = { id: "q" };
  t.mock.method(api, "permissions", async () => ({ data: [] }));
  t.mock.method(api, "questions", () => gate.promise);
  t.mock.method(api, "replyQuestion", async () => ({}));
  const human = new HumanInputController({ lifecycle: lifecycle(), actions: {} });
  human.installHumanInput("s", [], [question]);
  const loading = human.refreshHumanInput("s");
  await human.replyQuestion("s", "q", ["answer"]);
  gate.resolve({ data: [question] });
  await loading;
  assert.deepEqual(store.getState().sessionData.s.questions, []);
  assert.deepEqual(store.getState().attention.s, { permissions: 0, questions: 0 });
});

test("pending model projection belongs to its session and ends on confirmation", async (t) => {
  const gate = deferred();
  t.mock.method(api, "selectModel", () => gate.promise);
  const configuration = new ConfigurationController({ lifecycle: lifecycle(), workspace: { require() {} }, inspectorState: {}, actions: {} });
  const changing = configuration.setSelection("s", "p", "new", "high");
  const stale = { id: "s", selection: { provider: "p", model: "old" } };
  assert.equal(configuration.projectSelection(stale).selection.model, "new");
  const other = { id: "other", selection: stale.selection };
  assert.equal(configuration.projectSelection(other), other);
  gate.resolve({ data: { effective: { provider: "p", model: "effective", reasoningEffort: "high" } } });
  await changing;
  assert.equal(store.getState().sessions.s.selection.model, "effective");
  assert.equal(configuration.projectSelection(stale), stale);
});

test("upload enforces the server size limit before calling the API", async (t) => {
  const uploads = [];
  t.mock.method(api, "upload", async (...args) => { uploads.push(args); return { data: { uri: "file" } }; });
  store.setState((state) => ({ ...state, capabilities: { limits: { maxAttachmentBytes: 1048576 } } }));
  const inputs = new InputController({ lifecycle: lifecycle(), workspace: {}, actions: {} });
  await assert.rejects(inputs.upload({ size: 1048577 }, "s"), /1 MB server limit/);
  assert.equal(uploads.length, 0);
  const file = { size: 1048576 };
  assert.deepEqual(await inputs.upload(file, "s"), { uri: "file" });
  assert.deepEqual(uploads, [[file, "s"]]);
});

test("process input refreshes list then selected detail, and fences late actions", async (t) => {
  let epoch = 0;
  const calls = [];
  const gate = deferred();
  t.mock.method(api, "processStdin", async () => { calls.push("stdin"); return { data: { sessionID: "s" } }; });
  t.mock.method(api, "processSignal", () => gate.promise);
  t.mock.method(api, "processes", async () => { calls.push("list"); return { data: [{ processID: "p" }] }; });
  t.mock.method(api, "process", async () => { calls.push("detail"); return { data: { processID: "p", sessionID: "s", status: "done" } }; });
  store.setState((state) => ({ ...state,
    ui: { ...state.ui, inspector: { mode: "process", processID: "p" } },
    sessionData: { s: { processDetail: { processID: "p" } } },
  }));
  const inspector = new InspectorStateController({ lifecycleEpoch: () => epoch, refreshMessages() {}, notice() {} });
  await inspector.processes.sendProcessInput("p", "input");
  assert.deepEqual(calls, ["stdin", "list", "detail"]);
  assert.equal(store.getState().sessionData.s.processDetail.status, "done");
  const signaling = inspector.processes.signalProcess("p", "interrupt");
  epoch += 1;
  gate.resolve({ data: { sessionID: "s" } });
  await signaling;
  assert.deepEqual(calls, ["stdin", "list", "detail"]);
});


test("lifecycle retires store state before buffers, then cancels scheduled work before owners", () => {
  const order = [];
  const owner = new BrowserLifecycle({
    notice() {},
    resetConnection: ({ stopStream }) => order.push(["reset", stopStream]),
    clearBuffers: () => order.push(["buffers"]),
    retireOwners: () => { order.push(["owners"]); assert.equal(owner.scheduler.timers.size, 0); },
  });
  owner.scheduler.schedule("unused", 10000, () => assert.fail("retired timer ran"));
  const unsubscribe = store.subscribe(() => order.push(["store"]));
  try { owner.retire({ stopStream: true }); } finally { unsubscribe(); owner.scheduler.cancel(); }
  assert.deepEqual(order, [["reset", true], ["store"], ["buffers"], ["owners"]]);
  assert.equal(owner.epoch, 1);
});
