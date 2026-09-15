import assert from "node:assert/strict";
import { registerHooks } from "node:module";
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
globalThis.fetch = () => { throw new Error("No real network in workspace tests"); };

const { api, ApiError, YokeApi } = await import("../src/yoke/web/assets/js/api/client.js");
const { controller } = await import("../src/yoke/web/assets/js/state/controller.js");
const { store } = await import("../src/yoke/web/assets/js/state/store.js");
const { workspaceUnavailable } = await import("../src/yoke/web/assets/js/state/workspace/status.js");
const { reducePublicEvent } = await import("../src/yoke/web/assets/js/state/reducer.js");
const { getSessionComposerDraft, updateSessionComposerDraft, clearAllSessionComposerDrafts } = await import("../src/yoke/web/assets/js/state/session-composer-drafts.js");
const initial = store.getState();
const available = { status: "available", message: null };
const missing = { status: "missing", message: "Saved workspace /old is missing." };
const session = (workspace = available, directory = "/old") => ({
  id: "s", title: "Saved conversation", location: { directory }, workspace,
  selection: { provider: "p", model: "m" }, queue: { revision: 0 }, time: {},
});
const missingError = () => new ApiError(409, "session_workspace_unavailable", missing.message,
  { sessionID: "s", directory: "/old", status: "missing" });
const deferred = () => {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
};
function reset(workspace = available) {
  controller.retireLifecycle();
  clearAllSessionComposerDrafts();
  store.setState(() => ({
    ...initial, sessions: { s: session(workspace) }, sessionOrder: ["s"], archivedOrder: [],
    connection: { ...initial.connection, current: true },
    ui: { ...initial.ui, selectedSessionID: "s", newSession: false },
    sessionData: { s: { loaded: true, messageSnapshotLoaded: true,
      messages: [{ id: "historical", role: "user", content: "Saved transcript" }],
      queue: { revision: 0, items: [] } } },
  }));
}

test("workspace absence remains backward compatible and all unavailable statuses block execution", async () => {
  reset();
  assert.equal(workspaceUnavailable({ location: { directory: "/old" } }), null);
  for (const status of ["missing", "not_directory", "unreadable", "unconfigured", "invalid"]) {
    reset({ status, message: "Unavailable" });
    for (const action of [
      () => controller.submitPrompt("s", { text: "Do work" }),
      () => controller.compact("s"),
      () => controller.setSelection("s", "p", "m", "low"),
    ]) await assert.rejects(action, { code: "session_workspace_unavailable" });
  }
});

test("runtime error events retain lastError without guessing workspace status from prose", () => {
  reset();
  const state = reducePublicEvent(store.getState(), {
    type: "session.active.changed", sessionID: "s",
    data: { state: "error", lastError: "Workspace unavailable" },
  });
  assert.equal(state.active.s.lastError, "Workspace unavailable");
  assert.equal(workspaceUnavailable(state.sessions.s, state.active.s), null);
});

test("conflicting relocation leaves history, root caches and drafts untouched", async (t) => {
  reset(missing);
  updateSessionComposerDraft("s", { text: "Keep this draft" });
  const previous = store.getState();
  t.mock.method(api, "relocateSession", async () => {
    throw new ApiError(409, "session_workspace_conflict", "Workspace changed in another client.");
  });
  await assert.rejects(() => controller.workspace.relocate("s", "/new", "/old"), {
    code: "session_workspace_conflict",
  });
  assert.equal(store.getState(), previous);
  assert.equal(getSessionComposerDraft("s").text, "Keep this draft");
});

test("missing workspace leaves transcript loading independent of failing catalogs", async (t) => {
  reset(missing);
  store.setState((state) => ({ ...state, sessionData: {} }));
  t.mock.method(api, "messages", async () => ({ data: [{ id: "history", role: "user", content: "Readable" }] }));
  t.mock.method(api, "queue", async () => ({ data: { revision: 0, items: [] } }));
  t.mock.method(api, "permissions", async () => ({ data: [] }));
  t.mock.method(api, "questions", async () => ({ data: [] }));
  t.mock.method(api, "history", async () => ({ data: [], hasMore: false }));
  t.mock.method(api, "tools", async () => { throw missingError(); });
  await controller.loadSession("s");
  await assert.rejects(() => controller.refreshTools("s"));
  assert.equal(store.getState().sessionData.s.loaded, true);
  assert.equal(store.getState().sessionData.s.loadError, null);
  assert.equal(store.getState().sessionData.s.messages[0].content, "Readable");
});

test("typed deletion errors update workspace even after optimistic prompt rollback", async (t) => {
  reset();
  t.mock.method(api, "admitPrompt", async () => { throw missingError(); });
  t.mock.method(api, "queue", async () => ({ data: { revision: 0, items: [] } }));
  await assert.rejects(() => controller.submitPrompt("s", { text: "Keep this" }), { code: "session_workspace_unavailable" });
  assert.equal(store.getState().sessions.s.workspace.status, "missing");
  assert.equal(store.getState().sessionData.s.livePrompt, null);
  assert.equal(store.getState().sessionData.s.messages[0].id, "historical");
});

test("relocation keeps same ID and draft, clears root caches and rejects old in-flight catalogs", async (t) => {
  reset(missing);
  const old = deferred();
  const oldModels = deferred();
  const oldProviders = deferred();
  const oldSummary = deferred();
  t.mock.method(api, "tools", () => old.promise);
  t.mock.method(api, "models", () => oldModels.promise);
  t.mock.method(api, "providers", () => oldProviders.promise);
  t.mock.method(api, "getSession", () => oldSummary.promise);
  const pending = [controller.refreshTools("s"), controller.loadModels("/old", "p"),
    controller.loadProviders("/old"), controller.refreshSessionSummary("s")];
  updateSessionComposerDraft("s", { text: "Retain draft", attachments: [{ uri: "upload:test" }] });
  store.setState((state) => ({ ...state,
    models: { "/old:p:": ["old"], "/new:p:": ["cached"], "/other:p:": ["unrelated"] },
    providerCatalogs: { "/old": ["old"], "/new": ["cached"] },
    sessionData: { s: { ...state.sessionData.s, tools: ["old"], skills: { active: ["historical"] }, mcp: ["old"], fileDetail: { content: "old file" } } },
  }));
  t.mock.method(api, "relocateSession", async (...args) => {
    assert.deepEqual(args, ["s", "/new", "/old"]);
    return { data: session(available, "/new") };
  });
  t.mock.method(api, "tools", async () => ({ data: ["new tool"] }));
  t.mock.method(api, "models", async () => ({ data: ["new model"] }));
  t.mock.method(api, "providers", async () => ({ data: ["new provider"] }));
  t.mock.method(api, "sessionSkills", async () => ({ data: { active: ["historical"], available: ["new skill"] } }));
  t.mock.method(api, "sessionMcp", async () => { throw new Error("MCP catalog failed"); });
  t.mock.method(api, "resolveLocation", async (directory) => ({ data: { directory, name: "New project" } }));
  t.mock.method(api, "recentLocations", async () => ({ data: [{ directory: "/new" }] }));
  t.mock.method(controller, "resolveVisibleLocations", async () => {});
  t.mock.method(controller, "refreshSessionLists", async () => {});
  const result = await controller.workspace.relocate("s", "/new", "/old");
  old.resolve({ data: ["stale tool"] });
  oldModels.resolve({ data: ["stale model"] });
  oldProviders.resolve({ data: ["stale provider"] });
  oldSummary.resolve({ data: session(missing) });
  await Promise.all(pending);
  const state = store.getState();
  assert.equal(result.id, "s");
  assert.equal(state.sessions.s.location.directory, "/new");
  assert.deepEqual(state.sessionData.s.tools, ["new tool"]);
  assert.equal(state.models["/old:p:"], undefined);
  assert.deepEqual(state.models["/new:p:"], ["new model"]);
  assert.deepEqual(state.models["/other:p:"], ["unrelated"]);
  assert.equal(state.providerCatalogs["/old"], undefined);
  assert.equal(state.sessionData.s.fileDetail, null);
  assert.equal(state.sessionData.s.mcp, null);
  assert.equal(state.sessionData.s.messages[0].id, "historical");
  assert.equal(getSessionComposerDraft("s").text, "Retain draft");
  assert.equal(state.sessionData.s.loadError, undefined);
  assert.equal(state.locations["/new"].name, "New project");
  assert.equal(state.recentLocations[0].directory, "/new");
});

test("API reports typed errors, but ignores late old-directory failures after relocation", async (t) => {
  reset();
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ error: {
    code: "session_workspace_unavailable", message: missing.message, details: missingError().details,
  } }), { status: 409, headers: { "Content-Type": "application/json" } }));
  await assert.rejects(() => api.compact("s"));
  assert.equal(store.getState().sessions.s.workspace.status, "missing");
  const late = api.captureError();
  controller.workspace.invalidate("s", ["/old", "/new"]);
  store.setState((state) => ({ ...state, sessions: { s: session(available, "/new") } }));
  late(missingError());
  assert.equal(store.getState().sessions.s.workspace.status, "available");
});

test("browser API serializes relocation precondition and optional fork location", async () => {
  const client = new YokeApi();
  const requests = [];
  client.request = async (path, options) => { requests.push([path, JSON.parse(options.body)]); };
  await client.relocateSession("a/b", "/new", "/old");
  await client.forkSession("a/b", { location: { directory: "/fork" } });
  assert.deepEqual(requests, [
    ["/api/v1/session/a%2Fb/relocate", { directory: "/new", expectedDirectory: "/old" }],
    ["/api/v1/session/a%2Fb/fork", { location: { directory: "/fork" } }],
  ]);
});

// Render real HTM component templates with isolated hooks, no DOM or daemon.
let activeDriver;
globalThis.__workspaceHooks = {
  useState(initial) {
    const driver = activeDriver, index = driver.index++;
    if (!(index in driver.values)) driver.values[index] = typeof initial === "function" ? initial() : initial;
    return [driver.values[index], (value) => { driver.values[index] = typeof value === "function" ? value(driver.values[index]) : value; }];
  },
};
const root = new URL("../src/yoke/web/assets/", import.meta.url);
const hooks = registerHooks({
  load(url, context, next) {
    if (url === new URL("vendor/htm-preact.js", root).href) return {
      format: "module", shortCircuit: true,
      source: `import htm from ${JSON.stringify(new URL("vendor/htm.module.js", root).href)};
        export const html = htm.bind((type, props, ...children) => ({type, props: {...props, children}}));
        export const useState = (...args) => globalThis.__workspaceHooks.useState(...args);
        export const useRef = (value) => useState(() => ({current: value}))[0];
        export const useEffect = () => {}; export const useLayoutEffect = useEffect;
        export const useMemo = (fn) => fn();`,
    };
    return next(url, context);
  },
});
const { SessionComposer } = await import("../src/yoke/web/assets/js/session/composer.js");
const { WorkspaceNotice } = await import("../src/yoke/web/assets/js/session/workspace-notice.js");
const { MainView } = await import("../src/yoke/web/assets/js/session/session-view.js");
hooks.deregister();
function driver(component, props) {
  const state = { index: 0, values: [] };
  return () => { activeDriver = state; state.index = 0; return component(props); };
}
function nodes(value) {
  if (Array.isArray(value)) return value.flatMap(nodes);
  if (!value || typeof value !== "object") return [];
  return [value, ...nodes(value.props?.children)];
}
const text = (value) => Array.isArray(value) ? value.map(text).join("")
  : value && typeof value === "object" ? text(value.props?.children) : String(value ?? "");
const button = (tree, label) => nodes(tree).find((node) => node.type === "button" && text(node) === label);

test("unavailable view retains timeline and editable draft, disables send/model/compact", () => {
  reset(missing);
  updateSessionComposerDraft("s", { text: "Draft still readable" });
  const props = { sessionID: "s", session: session(missing), runtime: null, data: store.getState().sessionData.s };
  const tree = driver(SessionComposer, props)();
  assert.equal(nodes(tree).find((node) => node.props?.["aria-label"] === "Send message").props.disabled, true);
  assert.equal(nodes(tree).find((node) => node.type?.name === "ModelSelectionControl").props.disabled, true);
  const input = nodes(tree).find((node) => node.type === "textarea");
  assert.equal(input.props.value, "Draft still readable");
  assert.equal(input.props.disabled, false);
  const viewNode = driver(MainView, {})();
  const view = driver(viewNode.type, {})();
  assert.ok(nodes(view).some((node) => node.type?.name === "Timeline"));
  const headerNode = nodes(view).find((node) => node.type?.name === "SessionHeader");
  const header = driver(headerNode.type, headerNode.props)();
  assert.ok(nodes(header).filter((node) => node.type === "button" && text(node).includes("Compact")).every((node) => node.props.disabled));
  assert.equal(button(header, "Rename").props.disabled, false);
});

test("rejected prompt restores exact composer text and attachments", async (t) => {
  reset();
  const attachments = [{ uri: "upload:test", name: "image.png" }];
  updateSessionComposerDraft("s", { text: "Keep my prompt", attachments });
  t.mock.method(api, "admitPrompt", async () => { throw missingError(); });
  t.mock.method(api, "queue", async () => ({ data: { revision: 0, items: [] } }));
  const render = driver(SessionComposer, { sessionID: "s", session: session(), data: store.getState().sessionData.s });
  const send = nodes(render()).find((node) => node.props?.["aria-label"] === "Send message");
  await send.props.onClick();
  assert.equal(getSessionComposerDraft("s").text, "Keep my prompt");
  assert.deepEqual(getSessionComposerDraft("s").attachments, attachments);
  assert.equal(store.getState().sessions.s.workspace.status, "missing");
});

test("a rejected slash command never duplicates a draft still owned by the composer", async (t) => {
  reset();
  updateSessionComposerDraft("s", { text: "/skill historical", attachments: [] });
  t.mock.method(controller, "runSlashCommand", async () => { throw missingError(); });
  const render = driver(SessionComposer, { sessionID: "s", session: session(), data: store.getState().sessionData.s });
  await nodes(render()).find((node) => node.props?.["aria-label"] === "Send message").props.onClick();
  assert.equal(getSessionComposerDraft("s").text, "/skill historical");
});

test("relocation is explicit, uses picker and retains notice on busy failure", async (t) => {
  reset(missing);
  const render = driver(WorkspaceNotice, { session: session(missing), runtime: null });
  assert.equal(button(render(), "Relocate this session"), undefined);
  button(render(), "Relocate workspace").props.onClick();
  let tree = render();
  const picker = nodes(tree).find((node) => node.type?.name === "LocationPicker");
  assert.ok(picker);
  assert.equal(button(tree, "Relocate this session").props.disabled, true);
  picker.props.onChange("/new");
  t.mock.method(controller.workspace, "relocate", async (...args) => {
    assert.deepEqual(args, ["s", "/new", "/old"]);
    throw new ApiError(409, "session_workspace_busy", "Remove pending queue items first.");
  });
  await button(render(), "Relocate this session").props.onClick();
  tree = render();
  assert.match(text(tree), /Remove pending queue items first/);
  assert.equal(store.getState().sessions.s.location.directory, "/old");
});

test.after(() => controller.stop());
