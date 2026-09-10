import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";
import { groupTools, matchingLines, mcpCounts, mcpToolEnabled, mcpToolPatch, messagePreview, skillSections } from "../src/yoke/web/assets/js/inspector/config/logic.js";
import { defaultProcessFilter, outputText, selectedProcessDetail, visibleProcesses } from "../src/yoke/web/assets/js/inspector/process/logic.js";

const root = new URL("../src/yoke/web/assets/", import.meta.url);
const pending = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("tool search covers technical IDs, groups source and filters enabled state", () => {
  const tools = [{ name: "z", source: "MCP", enabled: true, capabilityID: "remote:read" }, { name: "a", source: "Built-in", enabled: false }, { name: "b", source: "Built-in", enabled: true }];
  assert.deepEqual(groupTools(tools, "", "all").map((group) => [group.source, group.tools.map((tool) => tool.name)]), [["Built-in", ["a", "b"]], ["MCP", ["z"]]]);
  assert.equal(groupTools(tools, "REMOTE:READ", "enabled")[0].tools[0].name, "z");
  assert.equal(groupTools(tools, "", "disabled")[0].tools[0].name, "a");
  assert.equal(groupTools(tools, "none", "all").length, 0);
  assert.deepEqual(tools.map((tool) => tool.name), ["z", "a", "b"]);
});

test("an optimistic toggle cannot hide its pending or failed control", () => {
  const tools = [{ name: "read", source: "Built-in", enabled: false }];
  assert.equal(groupTools(tools, "", "enabled", { read: { pending: true } }).length, 1);
  assert.equal(groupTools(tools, "", "enabled", { read: { error: "offline" } }).length, 1);
  assert.equal(groupTools(tools, "", "enabled", { read: { success: "Saved" } }).length, 0);
});

test("active skills remain visible outside available search and include active-only skills", () => {
  const result = skillSections({ active: [{ name: "old" }, { name: "active" }], available: [{ name: "active", description: "Instructions", sourcePath: "/skill" }, { name: "other", description: "Find me" }] }, "FIND");
  assert.deepEqual(result.active.map((skill) => skill.name), ["active", "old"]);
  assert.equal(result.active[0].sourcePath, "/skill");
  assert.deepEqual(result.available.map((skill) => skill.name), ["other"]);
});

test("MCP denylist overrides allowlist and empty allowlist means no tools", () => {
  assert.equal(mcpToolEnabled({ enabledTools: ["read"], disabledTools: ["read"] }, "read"), false);
  assert.equal(mcpToolEnabled({ enabledTools: [] }, "read"), false);
  assert.equal(mcpToolEnabled({ enabledTools: null }, "read"), true);
});

test("MCP toggles preserve uninspected policy names and never broaden an allowlist", () => {
  const server = { enabledTools: ["read", "unloaded"], disabledTools: ["read", "hidden"] };
  const enabled = mcpToolPatch(server, "read", true);
  assert.deepEqual(enabled, { enabledTools: ["read", "unloaded"], disabledTools: ["hidden"] });
  const disabled = mcpToolPatch({ ...server, ...enabled }, "read", false);
  assert.deepEqual(disabled, { enabledTools: ["unloaded"], disabledTools: ["hidden", "read"] });
  assert.deepEqual(mcpToolPatch({ enabledTools: null, disabledTools: [] }, "read", false), { disabledTools: ["read"] });
  assert.deepEqual(mcpToolPatch({ enabledTools: [], disabledTools: ["read"] }, "read", true), { enabledTools: ["read"], disabledTools: [] });
  assert.deepEqual(server.disabledTools, ["read", "hidden"]);
});

test("MCP capability counts reflect effective server enabled state", () => {
  const server = { enabled: true, tools: [{ name: "a" }, { name: "b" }], disabledTools: ["b"] };
  assert.deepEqual(mcpCounts(server), { enabled: 1, total: 2 });
  assert.deepEqual(mcpCounts({ ...server, enabled: false }), { enabled: 0, total: 2 });
});

test("context preview identifies non-text blocks without pretending they are text", () => {
  assert.equal(messagePreview({ content: [{ type: "text", text: "Hello\nworld" }, { type: "image", name: "photo" }, { type: "audio" }] }), "Hello world [image block] [audio block]");
  assert.equal(messagePreview({ content: [] }), "No content blocks");
});

test("file find is literal, case insensitive and uses disk line positions", () => {
  assert.deepEqual(matchingLines("one\nA.*\n\na.*\n", "a.*"), [1, 3]);
  assert.deepEqual(matchingLines("one", ""), []);
});

test("process defaults choose running only when present and retain chosen filters", () => {
  assert.equal(defaultProcessFilter([], null), "all");
  assert.equal(defaultProcessFilter([{ status: "exited" }], null), "all");
  assert.equal(defaultProcessFilter([{ status: "running" }], null), "running");
  assert.equal(defaultProcessFilter([], "running"), "running");
  assert.equal(defaultProcessFilter([{ status: "running" }], "all"), "all");
});

test("processes sort newest-started, preserve tied order, and search command/path/runtime ID", () => {
  const rows = [{ processID: "z", command: "old", startedAt: "2026-01-01", status: "running" }, { processID: "b", command: "new", cwd: "/repo", runtimeSessionID: 41, startedAt: "2026-01-02", status: "exited" }, { processID: "a", command: "tied", startedAt: "2026-01-02", status: "running" }];
  assert.deepEqual(visibleProcesses(rows, "all", "").map((row) => row.processID), ["b", "a", "z"]);
  assert.equal(visibleProcesses(rows, "all", "41")[0].processID, "b");
  assert.equal(visibleProcesses(rows, "all", "/REPO")[0].processID, "b");
  assert.deepEqual(visibleProcesses(rows, "running", "").map((row) => row.processID), ["a", "z"]);
  assert.deepEqual(rows.map((row) => row.processID), ["z", "b", "a"]);
});

test("watched process exit preserves detail, while a different requested ID hides stale detail", () => {
  const detail = { processID: "watched", status: "exited", output: { tail: "final output" } };
  assert.equal(selectedProcessDetail(detail, "watched"), detail);
  assert.equal(selectedProcessDetail(detail, "other"), null);
  assert.equal(outputText(detail), "final output");
  assert.equal(outputText({ status: "exited", output: { tail: "" } }), "No output.");
  assert.equal(outputText({ status: "running", output: { tail: "" } }), "Waiting for process output…");
});

// Isolated component tests use real HTM templates with a small hook driver.
// No controller, store, network, browser storage, or live sessions are imported.
globalThis.fetch = () => { throw new Error("Network access forbidden in support tests"); };
globalThis.__supportController = {};
let activeDriver;
globalThis.__supportHooks = {
  useState(initial) {
    const driver = activeDriver, index = driver.index++;
    if (!(index in driver.values)) driver.values[index] = typeof initial === "function" ? initial() : initial;
    return [driver.values[index], (value) => { driver.values[index] = typeof value === "function" ? value(driver.values[index]) : value; }];
  },
  useRef(value) { return this.useState(() => ({ current: value }))[0]; },
  useEffect(effect, deps) {
    const driver = activeDriver, index = driver.index++;
    const previous = driver.effects[index];
    if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) {
      previous?.cleanup?.();
      driver.effects[index] = { deps, effect, needsRun: true };
    }
  },
};
const hooks = registerHooks({
  load(url, context, nextLoad) {
    if (url === new URL("vendor/htm-preact.js", root).href) return {
      format: "module", shortCircuit: true,
      source: `import htm from ${JSON.stringify(new URL("vendor/htm.module.js", root).href)};
        const h = (type, props, ...children) => ({ type, props: { ...props, children } });
        export const html = htm.bind(h);
        export const useState = (...args) => globalThis.__supportHooks.useState(...args);
        export const useRef = (...args) => globalThis.__supportHooks.useRef(...args);
        export const useEffect = (...args) => globalThis.__supportHooks.useEffect(...args);
        export const useLayoutEffect = useEffect;`,
    };
    if (url === new URL("js/state/controller.js", root).href) return { format: "module", shortCircuit: true, source: "export const controller = globalThis.__supportController;" };
    return nextLoad(url, context);
  },
});
const { McpView } = await import("../src/yoke/web/assets/js/inspector/config/mcp.js");
const { ToolsView, SkillsView } = await import("../src/yoke/web/assets/js/inspector/config/catalog.js");
const { FileView, ContextView } = await import("../src/yoke/web/assets/js/inspector/config/documents.js");
const { ProcessView } = await import("../src/yoke/web/assets/js/inspector/process/view.js");
const { ProcessControls } = await import("../src/yoke/web/assets/js/inspector/process/controls.js");
const { ProcessOutput } = await import("../src/yoke/web/assets/js/inspector/process/output.js");
hooks.deregister();

function driver(component, initialProps) {
  const state = { index: 0, values: [], effects: [], props: initialProps };
  state.render = (patch = {}) => {
    state.props = { ...state.props, ...patch };
    activeDriver = state;
    state.index = 0;
    state.tree = component(state.props);
    activeDriver = null;
    return state.tree;
  };
  state.flush = () => { for (const item of state.effects) if (item?.needsRun) { item.needsRun = false; item.cleanup = item.effect(); } };
  state.unmount = () => { for (const item of state.effects) item?.cleanup?.(); };
  state.render();
  return state;
}
function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...nodes(tree.props?.children)];
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join(" ");
  return tree && typeof tree === "object" ? text(tree.props?.children) : String(tree ?? "");
}
const find = (tree, predicate) => { const node = nodes(tree).find(predicate); assert.ok(node, "Expected component control"); return node; };
const button = (tree, label) => find(tree, (node) => node.type === "button" && text(node).replace(/\s+/g, " ").trim() === label);
const labelled = (tree, label) => find(tree, (node) => node.props?.["aria-label"] === label);
const change = (node, value, checked) => node.props.onChange({ currentTarget: { value, checked } });
function preferences(initial = {}) {
  const result = { value: { search: "", filter: null, scrollTop: 0, mobilePane: "list", outputs: {}, wrap: true, ...initial } };
  result.patch = (patch) => { result.value = { ...result.value, ...(typeof patch === "function" ? patch(result.value) : patch) }; };
  return result;
}
function mcpFixture() {
  const server = { name: "fixture", enabled: true, scope: "global", transport: "stdio", status: "connected", enabledTools: ["read", "outside-page"], disabledTools: [], tools: [{ name: "read" }, { name: "write" }] };
  const view = driver(McpView, { sessionID: "fixture-only", data: { mcp: [server] }, preferences: preferences().value, patchPreferences() {} });
  const vnode = find(view.tree, (node) => typeof node.type === "function" && node.type.name === "McpServer");
  return driver(vnode.type, vnode.props);
}

test("MCP persistent edits stage until Apply and scope changes discard the draft", async () => {
  const writes = [];
  globalThis.__supportController.patchMcp = async (...args) => { writes.push(args); };
  const view = mcpFixture();
  change(labelled(view.tree, "Apply scope for fixture"), "repo"); view.render();
  change(labelled(view.tree, "Enable server fixture"), null, false); view.render();
  assert.equal(writes.length, 0);
  assert.match(text(view.tree), /Unsaved draft/);
  change(labelled(view.tree, "Apply scope for fixture"), "global"); view.render();
  assert.equal(labelled(view.tree, "Enable server fixture").props.checked, true);
  assert.equal(button(view.tree, "Apply to global config").props.disabled, true);
  change(labelled(view.tree, "Allow write on fixture"), null, true); view.render();
  await button(view.tree, "Apply to global config").props.onClick(); view.render();
  assert.deepEqual(writes, [["fixture-only", "fixture", { scope: "global", disabledTools: [], enabledTools: ["read", "outside-page", "write"] }]]);
  assert.equal(button(view.tree, "Apply to global config").props.disabled, true);
});

test("MCP session changes are immediate with pending feedback and failure leaves retry available", async () => {
  const request = pending(), writes = [];
  globalThis.__supportController.patchMcp = (...args) => { writes.push(args); return request.promise; };
  const view = mcpFixture();
  change(labelled(view.tree, "Allow read on fixture"), null, false); view.render();
  assert.deepEqual(writes[0][2], { scope: "session", disabledTools: ["read"], enabledTools: ["outside-page"] });
  assert.equal(labelled(view.tree, "Apply scope for fixture").props.disabled, true);
  assert.ok(nodes(view.tree).some((node) => node.props?.state?.pending));
  request.reject(new Error("fixture rejected")); await tick(); view.render();
  assert.equal(labelled(view.tree, "Allow read on fixture").props.disabled, false);
  assert.ok(nodes(view.tree).some((node) => node.props?.state?.error === "fixture rejected"));
});

test("MCP failed Apply keeps the staged policy for retry", async () => {
  let attempts = 0;
  globalThis.__supportController.patchMcp = async () => { attempts += 1; throw new Error("Session is busy"); };
  const view = mcpFixture();
  change(labelled(view.tree, "Apply scope for fixture"), "repo"); view.render();
  change(labelled(view.tree, "Enable server fixture"), null, false); view.render();
  await button(view.tree, "Apply to repository").props.onClick(); view.render();
  assert.equal(attempts, 1);
  assert.equal(labelled(view.tree, "Enable server fixture").props.checked, false);
  assert.equal(button(view.tree, "Apply to repository").props.disabled, false);
  assert.ok(nodes(view.tree).some((node) => node.props?.state?.error === "Session is busy"));
  button(view.tree, "Discard").props.onClick(); view.render();
  assert.equal(labelled(view.tree, "Enable server fixture").props.checked, true);
});

test("tool toggle calls controller unchanged and exposes a local failure", async () => {
  globalThis.__supportController.toggleTool = async (...args) => { assert.deepEqual(args, ["fixture-only", "read", false]); throw new Error("offline"); };
  const view = driver(ToolsView, { sessionID: "fixture-only", data: { tools: [{ name: "read", enabled: true, source: "built-in" }] }, preferences: preferences({ filter: "enabled" }).value, patchPreferences() {} });
  change(labelled(view.tree, "Enable read for this session"), null, false); await tick(); view.render();
  assert.ok(nodes(view.tree).some((node) => node.props?.state?.error === "offline"));
});

test("skill preview opens current-file drilldown without activation", async () => {
  const opens = [];
  globalThis.__supportController.openInspector = async (...args) => { opens.push(args); };
  globalThis.__supportController.activateSkill = () => { throw new Error("Preview must not activate"); };
  const view = driver(SkillsView, { sessionID: "fixture-only", data: { skills: { available: [{ name: "skill", sourcePath: "/repo/SKILL.md" }] } }, preferences: preferences().value, patchPreferences() {} });
  button(view.tree, "Preview current instructions").props.onClick(); await tick();
  assert.deepEqual(opens, [["file", { path: "/repo/SKILL.md", from: { mode: "skills" } }]]);
});

test("context shows bounds, chronological label and honest mixed blocks", () => {
  const view = driver(ContextView, { session: { id: "fixture-only" }, data: { context: { messages: [{ role: "user", content: [{ type: "text", text: "Actual text" }, { type: "image", name: "plot" }] }], retainedEntries: 2, totalEntries: 50, retainedChars: 120, maxChars: 1000, truncated: true } }, preferences: preferences().value, patchPreferences() {} });
  assert.match(text(view.tree), /not the full provider prompt/);
  assert.match(text(view.tree), /oldest to newest/);
  assert.match(text(view.tree), /Earlier content omitted/);
  assert.match(text(view.tree), /Actual text/);
  assert.match(text(view.tree), /Image contents are not rendered/);
  assert.equal(nodes(view.tree).filter((node) => node.type === "img").length, 0);
});

test("file uses controller Back, line numbers, literal find, and clipboard errors", async () => {
  let backs = 0;
  globalThis.__supportController.backInspector = async () => { backs += 1; };
  const prefs = preferences({ path: "/fixture", search: "two" });
  const view = driver(FileView, { data: { fileDetail: { path: "/fixture", content: "one\ntwo\nthree" } }, preferences: prefs.value, patchPreferences: prefs.patch });
  assert.match(text(view.tree), /Current file on disk/);
  assert.match(text(view.tree), /not a historical tool result/);
  assert.equal(nodes(view.tree).filter((node) => node.props?.["data-line"] !== undefined).length, 3);
  assert.match(text(view.tree), /1 \/ 1 matching lines/);
  button(view.tree, "Back").props.onClick(); await tick(); assert.equal(backs, 1);
  button(view.tree, "Copy file").props.onClick(); await tick(); view.render();
  assert.ok(nodes(view.tree).some((node) => node.props?.state?.error?.includes("Clipboard unavailable")));
});

test("process selection remains displayed outside Running after exit without a replacement load", () => {
  globalThis.window = { setInterval: () => 1, clearInterval() {} };
  globalThis.__supportController.loadProcess = () => { throw new Error("Must not replace watched selection"); };
  const prefs = preferences({ filter: "running" });
  const detail = { processID: "watched", command: "fixture command", status: "exited", runtimeSessionID: 1, startedAt: "2026-01-01", output: { tail: "final" } };
  const view = driver(ProcessView, { sessionID: "fixture-only", data: { processes: [detail], processDetail: detail }, preferences: prefs.value, patchPreferences: prefs.patch });
  view.flush();
  assert.match(text(view.tree), /Selected process outside this filter/);
  assert.ok(nodes(view.tree).some((node) => node.type === ProcessOutput && node.props.process === detail));
  assert.equal(prefs.value.selectedID, "watched");
  view.unmount();
});

test("return to a remembered process retains the narrow-screen list preference", () => {
  const prefs = preferences({ filter: "all", selectedID: "fixture", mobilePane: "list" });
  const detail = { processID: "fixture", command: "fixture", status: "exited", runtimeSessionID: 1 };
  const view = driver(ProcessView, { sessionID: "fixture-only", inspector: { processID: "fixture" }, data: { processes: [detail], processDetail: detail }, preferences: prefs.value, patchPreferences: prefs.patch });
  view.flush();
  assert.equal(prefs.value.mobilePane, "list");
  view.unmount();
});

test("process controls retain failed input, separate interrupt and confirm termination", async () => {
  const signals = [], inputs = [], request = pending();
  globalThis.__supportController.sendProcessInput = (...args) => { inputs.push(args); return request.promise; };
  globalThis.__supportController.signalProcess = async (...args) => { signals.push(args); };
  const view = driver(ProcessControls, { process: { processID: "fixture-only", runtimeSessionID: 9 } });
  find(view.tree, (node) => node.type === "textarea").props.onInput({ currentTarget: { value: "literal\n" } }); view.render();
  const action = button(view.tree, "Send input").props.onClick(); view.render();
  assert.equal(button(view.tree, "Interrupt").props.disabled, true);
  assert.equal(find(view.tree, (node) => node.type === "textarea").props.disabled, true);
  request.reject(new Error("fixture stdin failure")); await action; view.render();
  assert.deepEqual(inputs, [["fixture-only", "literal\n"]]);
  assert.equal(find(view.tree, (node) => node.type === "textarea").props.value, "literal\n");
  button(view.tree, "Interrupt").props.onClick(); await tick(); view.render();
  assert.deepEqual(signals, [["fixture-only", "interrupt"]]);
  button(view.tree, "Terminate…").props.onClick(); view.render();
  assert.equal(signals.length, 1);
  button(view.tree, "Cancel").props.onClick(); view.render();
  assert.equal(signals.length, 1);
  button(view.tree, "Terminate…").props.onClick(); view.render();
  await button(view.tree, "Terminate process").props.onClick();
  assert.deepEqual(signals[1], ["fixture-only", "terminate"]);
});

test("output restores reading position and Jump to live resumes following", () => {
  const prefs = preferences({ outputs: { fixture: { following: false, scrollTop: 40 } } });
  const view = driver(ProcessOutput, { process: { processID: "fixture", status: "running", output: { tail: "fixture output" } }, preferences: prefs.value, patchPreferences: prefs.patch });
  const pre = labelled(view.tree, "Process output");
  const viewport = { scrollTop: 0, scrollHeight: 1000, clientHeight: 200 };
  pre.props.ref.current = viewport;
  view.flush();
  assert.equal(viewport.scrollTop, 40);
  pre.props.onScroll({ currentTarget: viewport });
  assert.equal(prefs.value.outputs.fixture.following, false);
  button(view.tree, "Jump to live").props.onClick();
  assert.equal(viewport.scrollTop, 1000);
  assert.equal(prefs.value.outputs.fixture.following, true);
  view.render({ preferences: prefs.value }); view.flush();
  assert.equal(nodes(view.tree).some((node) => node.type === "button" && text(node).trim() === "Jump to live"), false);
});

test("following responds to replaced tails of the same length", () => {
  const prefs = preferences();
  const process = { processID: "fixture", status: "running", output: { tail: "old" } };
  const view = driver(ProcessOutput, { process, preferences: prefs.value, patchPreferences: prefs.patch });
  const viewport = { scrollTop: 0, scrollHeight: 1000 };
  labelled(view.tree, "Process output").props.ref.current = viewport;
  view.flush();
  viewport.scrollHeight = 1200;
  view.render({ process: { ...process, output: { tail: "new" } } }); view.flush();
  assert.equal(viewport.scrollTop, 1200);
});

test("successful input clears only the mounted control and duplicate submissions are ignored", async () => {
  const request = pending();
  let sends = 0;
  globalThis.__supportController.sendProcessInput = () => { sends += 1; return request.promise; };
  const view = driver(ProcessControls, { process: { processID: "fixture", runtimeSessionID: 1 } });
  view.flush();
  find(view.tree, (node) => node.type === "textarea").props.onInput({ currentTarget: { value: "sensitive input" } }); view.render();
  const send = button(view.tree, "Send input").props.onClick;
  const first = send(), duplicate = send();
  assert.equal(sends, 1);
  view.unmount();
  request.resolve(); await Promise.all([first, duplicate]);
  assert.equal(view.values[0], "sensitive input", "An unmounted control must not update after its request completes");
});
