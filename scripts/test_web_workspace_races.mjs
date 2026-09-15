import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
globalThis.localStorage = new MemoryStorage();
globalThis.sessionStorage = new MemoryStorage();
globalThis.window = {
  location: { pathname: "/session/s", search: "", hash: "", href: "http://fixture/session/s" },
  innerWidth: 1400, addEventListener() {}, removeEventListener() {},
};
globalThis.document = { querySelector() { return null; } };
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
globalThis.fetch = () => { throw new Error("No real network in workspace race tests"); };

const { api } = await import("../src/yoke/web/assets/js/api/client.js");
const { controller } = await import("../src/yoke/web/assets/js/state/controller.js");
const { store } = await import("../src/yoke/web/assets/js/state/store.js");
const { getSessionComposerDraft, updateSessionComposerDraft, clearAllSessionComposerDrafts } = await import("../src/yoke/web/assets/js/state/session-composer-drafts.js");
const initial = store.getState();
const available = { status: "available", message: null };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function fixture(t, rootless = false) {
  controller.retireLifecycle();
  clearAllSessionComposerDrafts();
  const root = mkdtempSync(join(tmpdir(), "yoke-web-workspace-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const oldDirectory = join(root, "old");
  const directory = join(root, "new");
  mkdirSync(oldDirectory);
  mkdirSync(directory);
  rmSync(oldDirectory, { recursive: true });
  assert.equal(existsSync(oldDirectory), false);
  const original = {
    id: "s", title: "Saved conversation", pinned: false,
    location: { directory: rootless ? null : oldDirectory },
    workspace: { status: rootless ? "unconfigured" : "missing", message: "Workspace unavailable" },
    selection: { provider: "p", model: "m" }, queue: { revision: 0 }, time: {},
  };
  let saved = original;
  const draft = { text: "Keep the other tab's draft", attachments: [{ uri: "upload:fixture" }] };
  updateSessionComposerDraft("s", draft);
  store.setState(() => ({
    ...initial, sessions: { s: original }, sessionOrder: ["s"], archivedOrder: [],
    connection: { ...initial.connection, current: true },
    ui: { ...initial.ui, selectedSessionID: "s", newSession: false },
    sessionData: { s: { loaded: true, messages: [{ id: "history", content: "Kept" }], fileDetail: { content: "old" } } },
    models: rootless ? {} : { [`${oldDirectory}:p:`]: ["old"] },
    providerCatalogs: rootless ? {} : { [oldDirectory]: ["old"] },
    locations: rootless ? {} : { [oldDirectory]: { directory: oldDirectory } },
  }));
  const scheduled = new Map();
  t.mock.method(controller, "schedule", (key, delay, task) => scheduled.set(key, task));
  t.mock.method(api, "getSession", async () => ({ data: saved }));
  const lists = t.mock.method(api, "listSessions", async ({ archived }) => ({ data: archived ? [] : [saved] }));
  t.mock.method(api, "tools", async (options) => {
    assert.equal(options.directory, directory);
    return { data: ["new tool"] };
  });
  t.mock.method(api, "models", async (options) => {
    assert.equal(options.directory, directory);
    return { data: ["new model"] };
  });
  t.mock.method(api, "providers", async (path) => {
    assert.equal(path, directory);
    return { data: ["new provider"] };
  });
  t.mock.method(api, "sessionSkills", async () => ({ data: ["new skill"] }));
  t.mock.method(api, "sessionMcp", async () => ({ data: ["new mcp"] }));
  t.mock.method(api, "resolveLocation", async (path) => {
    assert.equal(path, directory);
    return { data: { directory: path, name: "New project" } };
  });
  t.mock.method(api, "recentLocations", async () => ({ data: [{ directory }] }));
  const move = () => { saved = { ...saved, location: { directory }, workspace: available }; };
  t.mock.method(api, "relocateSession", async () => { move(); return { data: saved }; });
  return {
    oldDirectory, directory, original, scheduled, lists, move,
    patch(patch) { saved = { ...saved, ...patch }; return { data: saved }; },
    event() {
      controller.applyEvents([
        { type: "session.workspace.relocated", sessionID: "s", durable: { seq: 1 },
          data: { sessionID: "s", previousDirectory: original.location.directory, directory } },
        { type: "session.active.changed", sessionID: "s", data: { state: "idle" } },
      ]);
    },
    async drain() {
      while (scheduled.size) {
        const [key, task] = scheduled.entries().next().value;
        scheduled.delete(key);
        await task();
      }
    },
    assertRecovered() {
      const state = store.getState();
      assert.equal(existsSync(oldDirectory), false, "recovery must not recreate the old root");
      assert.equal(state.sessions.s.location.directory, directory);
      assert.equal(state.sessions.s.workspace.status, "available");
      assert.equal(getSessionComposerDraft("s").text, draft.text);
      assert.deepEqual(getSessionComposerDraft("s").attachments, draft.attachments);
      assert.equal(state.sessionData.s.messages[0].id, "history");
      assert.equal(state.sessionData.s.fileDetail, null);
      assert.deepEqual(state.sessionData.s.tools, ["new tool"]);
      assert.deepEqual(state.sessionData.s.skills, ["new skill"]);
      assert.deepEqual(state.sessionData.s.mcp, ["new mcp"]);
      assert.deepEqual(state.models[`${directory}:p:`], ["new model"]);
      assert.deepEqual(state.providerCatalogs[directory], ["new provider"]);
      assert.equal(state.models[`${oldDirectory}:p:`], undefined);
      assert.equal(state.providerCatalogs[oldDirectory], undefined);
      assert.equal(state.locations[oldDirectory], undefined);
      assert.equal(state.locations[directory].name, "New project");
      assert.equal(state.recentLocations[0].directory, directory);
      assert.ok(this.lists.mock.callCount() >= 2, "current and archived lists refreshed");
    },
  };
}

for (const patch of [{ title: "Renamed" }, { pinned: true }]) {
  test(`delayed ${Object.keys(patch)[0]} PATCH keeps relocated workspace`, async (t) => {
    const f = fixture(t);
    const reply = deferred(), started = deferred();
    let snapshot;
    t.mock.method(api, "patchSession", () => {
      snapshot = f.patch(patch);
      started.resolve();
      return reply.promise;
    });
    const pending = controller.patchSession("s", patch);
    await started.promise;
    await controller.workspace.relocate("s", f.directory, f.oldDirectory);
    reply.resolve(snapshot);
    await pending;
    f.assertRecovered();
    assert.ok(f.scheduled.has("summary:s"), "stale metadata requests authoritative refresh");
    await f.drain();
    f.assertRecovered();
    for (const [key, value] of Object.entries(patch)) assert.equal(store.getState().sessions.s[key], value);
    assert.equal(controller.sessionPendingMutations.has("s"), false);
  });
}

for (const [rollback, patch] of [
  ["snapshot", { pinned: true }], ["delayed GET", { pinned: true }],
  ["snapshot", { title: "Rejected rename" }], ["delayed GET", { title: "Rejected rename" }],
]) {
  test(`failed ${Object.keys(patch)[0]} PATCH ${rollback} rollback cannot restore missing root`, async (t) => {
    const f = fixture(t);
    const reply = deferred(), started = deferred(), recovery = deferred(), recoveryStarted = deferred();
    t.mock.method(api, "patchSession", () => { started.resolve(); return reply.promise; });
    const pending = assert.rejects(controller.patchSession("s", patch), /PATCH failed/);
    await started.promise;
    if (rollback === "delayed GET") {
      t.mock.method(api, "getSession", () => { recoveryStarted.resolve(); return recovery.promise; });
      reply.reject(new Error("PATCH failed"));
      await recoveryStarted.promise;
    }
    await controller.workspace.relocate("s", f.directory, f.oldDirectory);
    if (rollback === "snapshot") {
      t.mock.method(api, "getSession", async () => { throw new Error("Offline"); });
      reply.reject(new Error("PATCH failed"));
    } else recovery.resolve({ data: f.original });
    await pending;
    f.assertRecovered();
    for (const key of Object.keys(patch)) assert.equal(store.getState().sessions.s[key], f.original[key]);
    assert.ok(f.scheduled.has("summary:s"));
    t.mock.method(api, "getSession", async () => f.patch({}));
    await f.drain();
    f.assertRecovered();
  });
}

for (const rootless of [false, true]) {
  test(`other tab relocation event refreshes ${rootless ? "rootless" : "deleted-root"} session and retires old requests`, async (t) => {
    const f = fixture(t, rootless);
    const summary = deferred(), models = deferred(), providers = deferred(), tools = deferred(), lists = deferred();
    const oldMocks = [
      t.mock.method(api, "getSession", () => summary.promise),
      t.mock.method(api, "models", () => models.promise),
      t.mock.method(api, "providers", () => providers.promise),
      t.mock.method(api, "tools", () => tools.promise),
      t.mock.method(api, "listSessions", () => lists.promise),
    ];
    const old = [controller.refreshSessionSummary("s"), controller.loadModels(f.oldDirectory, "p"),
      controller.loadProviders(f.oldDirectory), controller.refreshTools("s"), controller.refreshSessionLists()];
    for (const mock of oldMocks) mock.mock.restore();
    // The remote tab changes server metadata. This tab receives only events,
    // never the initiating tab's relocation response.
    f.move();
    f.event();
    assert.ok(f.scheduled.size > 0);
    const retired = controller.workspace.guard();
    models.resolve({ data: ["stale model"] });
    providers.resolve({ data: ["stale provider"] });
    tools.resolve({ data: ["stale tool"] });
    summary.resolve({ data: f.original });
    lists.resolve({ data: [f.original] });
    await Promise.all(old);
    assert.equal(store.getState().models[`${f.oldDirectory}:p:`], undefined);
    assert.equal(store.getState().sessionData.s.tools, null);
    await f.drain();
    assert.equal(retired(), false, "summary observation advances the generation");
    f.assertRecovered();
  });
}

test("initiating tab also accepts its relocation event before the POST response", async (t) => {
  const f = fixture(t);
  const reply = deferred();
  t.mock.method(api, "relocateSession", () => { f.move(); return reply.promise; });
  const pending = controller.workspace.relocate("s", f.directory, f.oldDirectory);
  f.event();
  reply.resolve(f.patch({}));
  assert.equal((await pending).location.directory, f.directory);
  await f.drain();
  f.assertRecovered();
});

test("relocation event after the initiating POST still reloads invalidated catalogs", async (t) => {
  const f = fixture(t);
  await controller.workspace.relocate("s", f.directory, f.oldDirectory);
  f.assertRecovered();
  f.event();
  assert.equal(store.getState().sessionData.s.tools, null);
  await f.drain();
  f.assertRecovered();
});

test("workspace invalidation retries a pending sidebar search instead of leaving it loading", async (t) => {
  const f = fixture(t);
  f.lists.mock.restore();
  const first = deferred(), second = deferred(), secondStarted = deferred();
  let calls = 0;
  t.mock.method(api, "listSessions", () => {
    calls += 1;
    if (calls === 1) return first.promise;
    secondStarted.resolve();
    return second.promise;
  });
  const pending = controller.searchSessions("needle");
  controller.workspace.invalidate("s", [f.oldDirectory]);
  first.resolve({ data: [f.original] });
  await pending;
  await secondStarted.promise;
  assert.equal(store.getState().ui.searching, true);
  second.resolve({ data: [f.original] });
  for (let index = 0; index < 10 && store.getState().ui.searching; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(calls, 2);
  assert.equal(store.getState().ui.searching, false);
  assert.deepEqual(store.getState().ui.searchResults, ["s"]);
});

test("failed automatic search retry settles the current loading state", async (t) => {
  const f = fixture(t);
  f.lists.mock.restore();
  const first = deferred(), second = deferred(), secondStarted = deferred();
  let calls = 0;
  t.mock.method(api, "listSessions", () => {
    calls += 1;
    if (calls === 1) return first.promise;
    secondStarted.resolve();
    return second.promise;
  });
  const pending = controller.searchSessions("needle");
  controller.workspace.invalidate("s", [f.oldDirectory]);
  first.resolve({ data: [f.original] });
  await pending;
  await secondStarted.promise;
  second.reject(new Error("retry offline"));
  for (let index = 0; index < 10 && store.getState().ui.searching; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(calls, 2);
  assert.equal(store.getState().ui.search, "needle");
  assert.equal(store.getState().ui.searching, false);
});

test("failed retired search retry cannot clear a newer query", async (t) => {
  const f = fixture(t);
  f.lists.mock.restore();
  const first = deferred(), retry = deferred(), newer = deferred(), retryStarted = deferred();
  let calls = 0;
  t.mock.method(api, "listSessions", ({ search }) => {
    calls += 1;
    if (calls === 1) return first.promise;
    if (search === "needle") {
      retryStarted.resolve();
      return retry.promise;
    }
    return newer.promise;
  });
  const oldSearch = controller.searchSessions("needle");
  controller.workspace.invalidate("s", [f.oldDirectory]);
  first.resolve({ data: [f.original] });
  await oldSearch;
  await retryStarted.promise;
  const currentSearch = controller.searchSessions("newer");
  retry.reject(new Error("retired retry failed"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(store.getState().ui.search, "newer");
  assert.equal(store.getState().ui.searching, true);
  newer.resolve({ data: [f.original] });
  await currentSearch;
  assert.equal(store.getState().ui.searching, false);
  assert.deepEqual(store.getState().ui.searchResults, ["s"]);
});

test.after(() => controller.stop());
