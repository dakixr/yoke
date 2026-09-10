import assert from "node:assert/strict";
import { api, ApiError } from "../src/yoke/web/assets/js/api/client.js";
import { InspectorStateController } from "../src/yoke/web/assets/js/inspector/state/controller.js";
import { mergeLatestToolWindow } from "../src/yoke/web/assets/js/inspector/state/tool-activity.js";
import { inspectorPreferences, patchInspectorPreferences } from "../src/yoke/web/assets/js/inspector/state/view-memory.js";
import { backInspector, openInspector } from "../src/yoke/web/assets/js/inspector/state/navigation.js";
import { store } from "../src/yoke/web/assets/js/state/store.js";

const notices = [];
let epoch = 1;
let owner;
const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const data = () => store.getState().sessionData.s;
const call = (sequence) => ({ id: `opaque_${300 - sequence}`, sequence, toolName: "read_file", retention: "session", time: { started: null }, status: "ok", arguments: { raw: "{}" } });
const deferred = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function reset() {
  store.reset();
  notices.length = 0;
  epoch += 1;
  owner = new InspectorStateController({ lifecycleEpoch: () => epoch, refreshMessages: async () => {}, notice: (text) => notices.push(text) });
  store.setState((state) => ({ ...state, sessions: { s: { id: "s", location: { directory: "/fixture" } } }, sessionData: { s: { toolCalls: null } }, ui: { ...state.ui, selectedSessionID: "s" } }));
  api.toolCall = async (_sessionID, id) => ({ data: { ...call(1), id } });
  owner.beginSelection("tool");
}

test("latest window and every older page preserve canonical order", async () => {
  const calls = Array.from({ length: 205 }, (_, index) => call(index + 1));
  api.toolCalls = async (_sessionID, options) => {
    assert.equal(options.order, "latest");
    const end = options.cursor ? Number(options.cursor) : calls.length;
    const start = Math.max(0, end - options.limit);
    return { data: calls.slice(start, end), total: calls.length, cursor: { next: start ? String(start) : null } };
  };
  await owner.listToolCalls("s");
  assert.equal(data().toolCalls[0].sequence, 106);
  assert.equal(data().toolCalls.at(-1).sequence, 205);
  assert.equal(data().toolCallsTotal, 205);
  await owner.loadMoreToolCalls("s");
  await owner.loadMoreToolCalls("s");
  assert.deepEqual(data().toolCalls.map((item) => item.sequence), calls.map((item) => item.sequence));
  assert.equal(data().toolCallsCursor.next, null);
});

test("latest refresh keeps loaded earlier history and selection", async () => {
  const calls = Array.from({ length: 105 }, (_, index) => call(index + 1));
  owner.setSessionField("s", "toolCalls", calls);
  owner.setSessionField("s", "toolCallsCursor", { next: null });
  await owner.selectToolCall("s", calls[10].id);
  const detail = data().toolDetail;
  api.toolCalls = async () => ({ data: [...calls.slice(11), call(106), call(107)], total: 107, cursor: { next: "older" } });
  await owner.listToolCalls("s");
  assert.equal(data().toolCalls.length, 107);
  assert.equal(data().toolCallsCursor.next, null);
  assert.equal(data().toolDetail, detail);
  assert.equal(store.getState().ui.inspector.callID, calls[10].id);
});

test("changing selected call does not discard an in-flight list", async () => {
  const response = deferred();
  api.toolCalls = () => response.promise;
  const refresh = owner.listToolCalls("s");
  await owner.selectToolCall("s", call(1).id);
  response.resolve({ data: [call(1), call(2)], cursor: { next: null }, total: 2 });
  await refresh;
  assert.equal(data().toolCalls.length, 2);
  assert.equal(data().toolDetail.id, call(1).id);
});

test("returning from another tab restores selection and per-session view memory", async () => {
  await owner.selectToolCall("s", "selected-call");
  patchInspectorPreferences("s", "tool", { search: "config.py", scrollTop: 315, wrap: false });
  owner.beginSelection("skills");
  const returned = owner.beginSelection("tool");
  assert.equal(returned.inspector.callID, "selected-call");
  assert.equal(data().toolDetail.id, "selected-call");
  assert.deepEqual(inspectorPreferences(store.getState(), "s", "tool"), { search: "config.py", scrollTop: 315, wrap: false });
  assert.deepEqual(inspectorPreferences(store.getState(), "other", "tool"), {});
  owner.close();
  assert.equal(owner.beginSelection("tool").inspector.callID, "selected-call");
});

test("deep linked missing calls stay selected without being fabricated in the page", async () => {
  api.toolCalls = async () => ({ data: [call(150), call(151)], total: 151, cursor: { next: "earlier" } });
  owner.beginSelection("tool", { callID: "outside-page" });
  await Promise.all([owner.listToolCalls("s"), owner.loadToolCall("s", "outside-page")]);
  assert.equal(data().toolCalls.some((item) => item.id === "outside-page"), false);
  assert.equal(data().toolDetail.id, store.getState().ui.inspector.callID);
});

test("closing or replacing a session rejects old history responses", async () => {
  const response = deferred();
  api.toolCalls = () => response.promise;
  const refresh = owner.listToolCalls("s");
  owner.close();
  response.resolve({ data: [call(1)], total: 1, cursor: { next: null } });
  await refresh;
  assert.equal(data().toolCalls, null);
});

test("old page requests recover from an invalid cursor without changing selection", async () => {
  owner.setSessionField("s", "toolCallsCursor", { next: "expired" });
  await owner.selectToolCall("s", "selected-call");
  api.toolCalls = async (_sessionID, options) => {
    if (options.cursor) throw new ApiError(400, "invalid_cursor_anchor", "Cursor no longer exists");
    return { data: [call(3)], total: 3, cursor: { next: null } };
  };
  await owner.loadMoreToolCalls("s");
  assert.equal(data().toolCalls[0].sequence, 3);
  assert.equal(store.getState().ui.inspector.callID, "selected-call");
  assert.match(notices[0], /history changed/i);
});

test("a newly selected detail rejects an older success and an older error", async () => {
  const old = deferred();
  api.toolCall = async (_sessionID, id) => id === "old" ? old.promise : { data: { ...call(2), id } };
  const pending = owner.selectToolCall("s", "old");
  await owner.selectToolCall("s", "new");
  old.reject(new Error("Old failure"));
  await pending;
  assert.equal(data().toolDetail.id, "new");
  assert.equal(data().toolDetailError, null);
});

test("current detail failures are explicit and recover on retry", async () => {
  api.toolCall = async () => { throw new Error("Call no longer retained"); };
  await assert.rejects(owner.selectToolCall("s", "missing"), /no longer retained/);
  assert.equal(data().toolDetailError, "Call no longer retained");
  api.toolCall = async () => ({ data: { ...call(2), id: "missing" } });
  await owner.loadToolCall("s", "missing");
  assert.equal(data().toolDetailError, null);
});

test("newest merges never connect disjoint history windows silently", () => {
  const merged = mergeLatestToolWindow([call(1)], [call(105)]);
  assert.equal(merged.replacedOlder, true);
  assert.deepEqual(merged.calls, [call(105)]);
  assert.deepEqual(mergeLatestToolWindow(null, [call(1)]).calls, [call(1)]);
});

test("file drilldown Back restores its origin and cannot display a stale file", async () => {
  const host = {
    inspectorState: owner, clearNotice() {}, setSessionField: (...args) => owner.setSessionField(...args),
    listToolCalls: (...args) => owner.listToolCalls(...args), loadToolCall: (...args) => owner.loadToolCall(...args),
    closeInspector: () => owner.close(), openInspector: (mode, payload) => openInspector(host, mode, payload),
  };
  api.toolCalls = async () => ({ data: [call(1)], total: 1, cursor: { next: null } });
  api.fsRead = async (_directory, path) => `content of ${path}`;
  await owner.selectToolCall("s", call(1).id);
  await openInspector(host, "file", { path: "current.txt", from: { mode: "tool", callID: call(1).id } });
  assert.equal(data().fileDetail.content, "content of current.txt");
  await backInspector(host);
  assert.equal(store.getState().ui.inspector.callID, call(1).id);
  const file = deferred();
  api.fsRead = () => file.promise;
  const opening = openInspector(host, "file", { path: "different.txt", from: { mode: "tool", callID: call(1).id } });
  assert.equal(data().fileDetail, null);
  await backInspector(host);
  file.resolve("late file response");
  await opening;
  assert.equal(data().fileDetail, null);
});

test("revealing HEAD loads actual earlier pages and stops when inspector closes", async () => {
  owner.beginSelection("tree");
  const newest = { revision: 1, leafID: "head", entries: [{ id: "latest" }], cursor: { next: "earlier" } };
  owner.installLatestTree("s", newest);
  api.tree = async () => ({ data: { ...newest, entries: [{ id: "head", current: true }], cursor: { next: null } } });
  assert.equal((await owner.revealTreeHead("s")).entries[0].id, "head");
  owner.installLatestTree("s", { ...newest, revision: 2 });
  const pending = deferred();
  api.tree = () => pending.promise;
  const reveal = owner.revealTreeHead("s");
  owner.close();
  pending.resolve({ data: { ...newest, revision: 2, entries: [{ id: "head", current: true }], cursor: { next: null } } });
  assert.equal(await reveal, null);
});

test("renumbered branch overlaps reset the older cursor without hiding the new branch", async () => {
  const old = Array.from({ length: 200 }, (_, index) => call(index + 1));
  owner.setSessionField("s", "toolCalls", old);
  owner.setSessionField("s", "toolCallsCursor", { next: null });
  await owner.selectToolCall("s", old[9].id);
  const incoming = old.slice(100).map((item) => ({ ...item, sequence: item.sequence + 60 }));
  api.toolCalls = async () => ({ data: incoming, total: 260, cursor: { next: "new-branch-page" } });
  await owner.listToolCalls("s");
  assert.deepEqual(data().toolCalls, incoming);
  assert.equal(data().toolCallsCursor.next, "new-branch-page");
  assert.equal(data().toolCallsWindowChanged, true);
  assert.equal(store.getState().ui.inspector.callID, old[9].id);
});

test("first opening selects latest but runtime details keep their loaded ordinal", async () => {
  owner.setSessionField("s", "toolDetail", null);
  api.toolCalls = async () => ({ data: [call(237)], total: 237, cursor: { next: "earlier" } });
  api.toolCall = async (_sessionID, id) => ({ data: { ...call(237), id, sequence: null } });
  const host = {
    inspectorState: owner, clearNotice() {},
    listToolCalls: (...args) => owner.listToolCalls(...args),
    loadToolCall: (...args) => owner.loadToolCall(...args),
    selectToolCall: (...args) => owner.selectToolCall(...args),
  };
  await openInspector(host, "tool");
  assert.equal(store.getState().ui.inspector.callID, call(237).id);
  assert.equal(data().toolDetail.sequence, 237);
});

test("runtime process links clear a remembered unrelated process", async () => {
  owner.beginSelection("process");
  api.process = async (processID) => ({ data: { processID, sessionID: "s", status: "running" } });
  await owner.loadProcess("previous");
  const host = {
    inspectorState: owner, clearNotice() {},
    refreshProcesses: async () => owner.setSessionField("s", "processes", []),
    loadProcess: (...args) => owner.loadProcess(...args),
  };
  const opening = openInspector(host, "process", { runtimeSessionID: 999, from: { mode: "tool", callID: "origin" } });
  assert.equal(data().processDetail, null);
  assert.equal(store.getState().ui.inspector.processID, null);
  await opening;
  assert.equal(data().processDetail, null);
  assert.match(data().inspectorErrors.process, /no longer retained/);
});

test("explicit process selection cancels a pending runtime-ID resolution", async () => {
  const list = deferred();
  api.process = async (processID) => ({ data: { processID, sessionID: "s", status: "running" } });
  const host = {
    inspectorState: owner, clearNotice() {},
    refreshProcesses: async () => { await list.promise; owner.setSessionField("s", "processes", [{ processID: "older-target", runtimeSessionID: 4 }]); },
    loadProcess: (...args) => owner.loadProcess(...args),
  };
  const opening = openInspector(host, "process", { runtimeSessionID: 4 });
  await owner.loadProcess("newer-selection");
  list.resolve();
  await opening;
  assert.equal(data().processDetail.processID, "newer-selection");
  assert.equal(store.getState().ui.inspector.processID, "newer-selection");
});

test("renewed A to B to A process intent wins across outstanding requests", async () => {
  owner.beginSelection("process");
  const a = deferred();
  const b = deferred();
  api.process = (id) => id === "A" ? a.promise : b.promise;
  const firstA = owner.loadProcess("A");
  const firstB = owner.loadProcess("B");
  const renewedA = owner.loadProcess("A");
  b.resolve({ data: { processID: "B", sessionID: "s" } });
  await firstB;
  assert.equal(data().processDetail, null);
  a.resolve({ data: { processID: "A", sessionID: "s" } });
  await Promise.all([firstA, renewedA]);
  assert.equal(data().processDetail.processID, "A");
  assert.equal(store.getState().ui.inspector.processID, "A");
});

for (const { name, fn } of tests) {
  reset();
  await fn();
  owner.invalidateLifecycle();
  console.log(`PASS ${name}`);
}
console.log(JSON.stringify({ tests: tests.length }));
