import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { register } from "node:module";
import { sortToolCallsChronologically } from "../src/yoke/web/assets/js/inspector/tool-logic.js";
import { callOutcome, formatDuration, formatTimestamp } from "../src/yoke/web/assets/js/inspector/activity/presenters.js";
import { callArgumentState, capturedOutput, compactCallSignature, projectedResultValue } from "../src/yoke/web/assets/js/inspector/activity/data.js";
import {
  filterCalls, listScrollPosition, loadedScope, nearBottom, newCallCount,
  restoredScrollTop, selectedOutsideWindow, toolSearchText,
} from "../src/yoke/web/assets/js/inspector/activity/logic.js";

const call = (toolName, args = {}, extra = {}) => ({
  id: "call-z", toolName, status: "ok", arguments: { raw: JSON.stringify(args) }, ...extra,
});
const ids = (calls) => calls.map((item) => item.id);
const tests = [];
const test = (name, run) => tests.push({ name, run });

test("canonical sequence overrides timestamps and opaque IDs", () => {
  const calls = [
    { id: "a", sequence: 3, time: { started: "2025-01-01" } },
    { id: "z", sequence: 1, time: { started: "2025-01-03" } },
    { id: "x", sequence: 2 },
  ];
  assert.deepEqual(ids(sortToolCallsChronologically(calls)), ["z", "x", "a"]);
  assert.deepEqual(ids(calls), ["a", "z", "x"]);
});

test("missing chronology never invents opaque ID order", () => {
  assert.deepEqual(ids(sortToolCallsChronologically([{ id: "z" }, { id: "a" }])), ["z", "a"]);
  assert.deepEqual(ids(sortToolCallsChronologically([
    { id: "new", time: { started: "2025-02-01" } },
    { id: "z", time: { started: "2025-01-01" } },
    { id: "a", time: { started: "2025-01-01" } },
  ])), ["z", "a", "new"]);
});

test("compact signatures show actual tool names and arguments", () => {
  assert.equal(compactCallSignature(call("read", { path: "src/main.py", offset: 120, limit: 80 })), 'read(path="src/main.py", offset=120, limit=80)');
  const executed = call("exec_command", { cmd: "old" }, { arguments: { raw: '{"cmd":"old"}', executed: { cmd: "uv run pytest", tty: false, workdir: "/repo" } } });
  assert.equal(compactCallSignature(executed), 'exec_command(cmd="old")');
  const long = compactCallSignature(call("apply_patch", { input: "line\n".repeat(100) }), 80);
  assert.ok(long.length <= 80);
  assert.ok(long.startsWith("apply_patch(input="));
});

test("call argument state shows model-sent values and hides execution normalization", () => {
  const subject = call("read", {}, { arguments: { raw: '{"path":"./file","dropped":9}', executed: { path: "/repo/file", limit: 150, follow: false } } });
  assert.deepEqual(callArgumentState(subject).shown, { path: "./file", dropped: 9 });
  assert.deepEqual(
    callArgumentState(call("read", {}, { arguments: { raw: null, executed: { path: "/repo/file", limit: 150 } } })).shown,
    { path: "/repo/file", limit: 150 },
  );
});

test("captured output remains available while a call is running", () => {
  const detail = call("exec_command", {}, { outputChunks: [{ text: "done" }] });
  assert.equal(capturedOutput(detail), "done");
});

test("JSON projections render structurally while TOON stays text", () => {
  assert.deepEqual(projectedResultValue('{"ok":true,"output":"done"}'), {
    kind: "json", value: { ok: true, output: "done" }, text: '{"ok":true,"output":"done"}',
  });
  assert.deepEqual(projectedResultValue("ok: true\npaths[1]: a.py"), {
    kind: "text", value: null, text: "ok: true\npaths[1]: a.py",
  });
  assert.equal(projectedResultValue(null), null);
});

test("outcome metadata stays technical and no-match searches stay successful", () => {
  assert.equal(callOutcome(call("exec_command", {}, { result: { exit_code: 2 } })).state, "failed");
  assert.equal(callOutcome(call("rg", {}, { result: { ok: true, exit_code: 1, output: [] } })).label, "No matches");
  assert.equal(callOutcome(call("fd", {}, { result: { ok: true, exit_code: 2 } })).state, "failed");
  assert.equal(callOutcome(call("read", {}, { status: "running" })).state, "running");
  assert.equal(callOutcome(call("read", {}, { status: "cancelled" })).state, "cancelled");
});

test("search reads actual call and result payloads", () => {
  const calls = [call("read", { path: "src/a.py" }, { id: "z" }), call("rg", { query: "needle" }, { id: "a", result: { error: "denied" } })];
  assert.deepEqual(ids(filterCalls(calls, "SRC")), ["z"]);
  assert.deepEqual(ids(filterCalls(calls, "denied")), ["a"]);
  assert.equal(toolSearchText(calls[1]).includes("needle"), true);
  assert.equal(loadedScope(100, 413, 1, true), "100 of 413 calls loaded. 1 match loaded calls.");
  assert.equal(selectedOutsideWindow(calls, [calls[0]], "a"), "Selected call is hidden by filters");
});

test("new calls and list scroll preserve older investigations", () => {
  const calls = [{ id: "z", sequence: 1 }, { id: "a", sequence: 2 }, { id: "b", sequence: 3 }];
  assert.equal(newCallCount(calls, "a"), 1);
  assert.equal(newCallCount([{ id: "earlier" }, ...calls], "b"), 0);
  assert.equal(newCallCount(calls, "expired", 1), 2);
  const node = { scrollTop: 250, scrollHeight: 1000, clientHeight: 300 };
  assert.equal(nearBottom(node), false);
  assert.equal(restoredScrollTop(node, 250, false), 250);
  assert.equal(restoredScrollTop(node, 250, true), 700);
  const row = (id, top, bottom) => ({ dataset: { callId: id }, getBoundingClientRect: () => ({ top, bottom }) });
  const list = { scrollTop: 0, getBoundingClientRect: () => ({ top: 100 }), querySelectorAll: () => [row("z", 80, 99), row("a", 99, 150)] };
  assert.deepEqual(listScrollPosition(list), { scrollTop: 0, anchorID: "a", anchorOffset: -1 });
});

test("time and duration remain separate", () => {
  assert.equal(formatDuration(250), "250 ms");
  assert.equal(formatDuration(1500), "1.5 s");
  assert.equal(formatDuration(61000), "1m 01s");
  assert.equal(formatDuration(null), "duration unavailable");
  assert.equal(formatTimestamp(null), "time unavailable");
  assert.equal(formatTimestamp("invalid"), "time unavailable");
});

test("tool activity has no intent layer, status dropdown, raw toggle, or disclosures", async () => {
  const base = new URL("../src/yoke/web/assets/js/inspector/", import.meta.url);
  const source = await readFile(new URL("tool.js", base), "utf8");
  const detail = await readFile(new URL("activity/detail.js", base), "utf8");
  const list = await readFile(new URL("activity/list.js", base), "utf8");
  assert.match(source, /loadMoreToolCalls/);
  assert.match(source, /showLatestToolCalls/);
  assert.doesNotMatch(source, /status:/);
  assert.doesNotMatch(detail, /presentCall|ActivityDisclosure|Show raw|Show formatted|Context before call|Open process|Open current file/);
  assert.doesNotMatch(list, /<select|Intent \/ tool|All statuses/);
  assert.match(list, /Oldest to newest/);
  assert.match(list, /Search tool calls/);
  assert.match(detail, />Call</);
  assert.match(detail, />Result</);
});

test("rendered detail is call plus result and keeps off-window selection", async () => {
  const dataURL = (source) => `data:text/javascript,${encodeURIComponent(source)}`;
  const htmURL = new URL("../src/yoke/web/assets/vendor/htm.module.js", import.meta.url).href;
  const hooksURL = dataURL(`
    import htm from ${JSON.stringify(htmURL)};
    export const html = htm.bind((type, props, ...children) => ({ type, props: { ...props, children } }));
    export const useEffect = () => {};
    export const useLayoutEffect = () => {};
    export const useMemo = (fn) => fn();
    export const useRef = (current) => ({ current });
    export const useState = (value) => [value, () => {}];
  `);
  const controllerURL = dataURL(`export const controller = { selectToolCall: async () => {}, notice: () => {} };`);
  const preferencesURL = dataURL(`export function useInspectorPreferences(sessionID, mode, defaults) {
    const current = { ...defaults, ...globalThis.activityPreferences };
    return [current, (patch) => { globalThis.activityPreferences = { ...current, ...(typeof patch === "function" ? patch(current) : patch) }; }];
  }`);
  register(dataURL(`export async function resolve(specifier, context, next) {
    const result = await next(specifier, context);
    if (result.url.endsWith("/vendor/htm-preact.js")) return { url: ${JSON.stringify(hooksURL)}, shortCircuit: true };
    if (result.url.endsWith("/js/state/controller.js")) return { url: ${JSON.stringify(controllerURL)}, shortCircuit: true };
    if (result.url.endsWith("/inspector/state/preferences.js")) return { url: ${JSON.stringify(preferencesURL)}, shortCircuit: true };
    return result;
  }`));
  const { ToolInspector } = await import("../src/yoke/web/assets/js/inspector/tool.js");
  const render = (node) => {
    if (Array.isArray(node)) return node.map(render).flat();
    if (!node || typeof node !== "object") return node;
    if (typeof node.type === "function") return render(node.type(node.props));
    return { ...node, children: render(node.props.children || []) };
  };
  const nodes = (tree) => Array.isArray(tree) ? tree.flatMap(nodes) : tree && typeof tree === "object" ? [tree, ...nodes(tree.children)] : [];
  const content = (tree) => Array.isArray(tree) ? tree.map(content).join("") : tree && typeof tree === "object" ? content(tree.children) : tree == null || typeof tree === "boolean" ? "" : String(tree);
  globalThis.activityPreferences = { callID: "outside", pane: "detail", following: false };
  const selected = call("exec_command", {}, {
    id: "outside", status: "failed", sequence: 17,
    arguments: { raw: '{"cmd":"pytest"}', executed: { cmd: "uv run pytest", tty: false } },
    result: { ok: false, error: "Permission denied", exit_code: 2, output: "Permission denied", internal_runtime_field: "noise" },
    resultProjection: '{"ok":false,"output":"Permission denied","exit_code":2,"error":"Permission denied"}',
    time: { started: "2025-01-01T12:00:00Z", durationMs: 2200 },
  });
  const props = { sessionID: "session-a", inspector: { callID: "outside" }, data: { toolCalls: [call("read", { path: "b.py" })], toolDetail: selected, toolCallsTotal: 200, toolCallsCursor: { next: "older" } } };
  const tree = render(ToolInspector(props));
  const all = nodes(tree);
  assert.equal(all.filter((node) => node.props["data-call-id"] === "outside" && node.props["aria-current"] === "true").length, 1);
  const text = content(tree);
  assert.ok(text.includes("Selected call is outside loaded history"));
  assert.ok(text.includes("Call"));
  assert.ok(text.includes("exec_command"));
  assert.ok(text.includes("pytest"));
  assert.equal(text.includes("uv run pytest"), false);
  assert.equal(text.includes("Normalized before execution"), false);
  assert.ok(text.includes("Result"));
  assert.ok(text.includes("Permission denied"));
  assert.equal(text.includes("internal_runtime_field"), false);
  assert.ok(all.some((node) => node.props.class?.includes?.("activity-projected-json")));
  assert.ok(all.some((node) => node.props.class?.includes?.("tool-value__key")));
  assert.equal(all.filter((node) => node.type === "details").length, 0);
  assert.equal(all.filter((node) => node.type === "select").length, 0);
  assert.equal(text.includes("Intent"), false);
  assert.equal(text.includes("Context before call"), false);
  const back = all.find((node) => node.type === "button" && content(node) === "Back to tool activity");
  back.props.onClick();
  assert.equal(globalThis.activityPreferences.pane, "list");
});

test("chat preserves tool outcomes through live, saved, reloaded, and orphaned rows", async () => {
  // Reuse the isolated HTM/hook loader installed by the detail rendering test.
  globalThis.sessionStorage = { getItem: () => null };
  const { Timeline } = await import("../src/yoke/web/assets/js/session/timeline.js");
  const { reducePublicEvent } = await import("../src/yoke/web/assets/js/state/reducer.js");
  const render = (node) => {
    if (Array.isArray(node)) return node.map(render).flat();
    if (!node || typeof node !== "object") return node;
    if (typeof node.type === "function") return render(node.type(node.props));
    return { ...node, children: render(node.props.children || []) };
  };
  const nodes = (tree) => Array.isArray(tree) ? tree.flatMap(nodes) : tree && typeof tree === "object" ? [tree, ...nodes(tree.children)] : [];
  const content = (tree) => Array.isArray(tree) ? tree.map(content).join("") : tree && typeof tree === "object" ? content(tree.children) : tree == null || typeof tree === "boolean" ? "" : String(tree);
  const cases = [
    ["read", { ok: false, error: "Permission denied" }, "failed"],
    ["mcp.read", { isError: true }, "failed"],
    ["exec_command", { ok: true, exit_code: 2 }, "failed"],
    ["exec_command", { ok: true, exitCode: "1" }, "failed"],
    ["exec_command", { ok: true, returncode: -9 }, "failed"],
    ["exec_command", { ok: true, timed_out: true }, "failed"],
    ["read", { ok: false, cancelled: true }, "cancelled"],
    ["read", { ok: true, content: "error is just file content" }, "completed"],
    ["rg", { ok: true, exit_code: 1 }, "completed"],
    ["grep", { ok: true, exit_code: 1 }, "completed"],
    ["fd", { ok: true, exit_code: 1 }, "completed"],
    ["rg", { ok: false, exit_code: 1 }, "failed"],
    ["rg", { ok: true, exit_code: 2 }, "failed"],
  ];
  const check = (data, expected, context) => {
    const tree = render(Timeline({ sessionID: "s", data }));
    const rows = nodes(tree).filter((node) => node.props["data-tool-call-id"] === "c");
    assert.equal(rows.length, 1, context);
    assert.ok(rows[0].props.class.includes(`tool-line--${expected}`), context);
    const glyph = nodes(rows[0]).find((node) => node.props.class === "tool-line__glyph");
    const label = nodes(rows[0]).find((node) => node.props.class === "tool-line__state");
    assert.equal(content(glyph), { failed: "×", cancelled: "·", completed: "✓", running: "↳", pending: "↳" }[expected], context);
    assert.equal(content(label), { failed: "failed", cancelled: "cancelled", completed: "done", running: "working", pending: "queued" }[expected], context);
  };
  for (const [name, result, expected] of cases) {
    const assistant = { id: "a", type: "assistant", toolCalls: [{ id: "c", name, arguments: "{}" }] };
    const saved = { id: "t", type: "tool", callID: "c", result: JSON.stringify(result) };
    const started = reducePublicEvent({ sessionData: { s: {} } }, {
      type: "session.tool.started", sessionID: "s", data: { tool_call_id: "c", tool_name: name },
    });
    check(started.sessionData.s, "running", `${name} running`);
    check({ messages: [assistant] }, "pending", `${name} queued`);
    const ended = reducePublicEvent(started, {
      type: "session.tool.ended", sessionID: "s", data: { tool_call_id: "c", ok: result.ok ?? true, result },
    });
    const liveTools = ended.sessionData.s.liveTools;
    check({ liveTools }, expected, `${name} live tail`);
    check({ messages: [assistant], liveTools }, expected, `${name} live call`);
    check({ messages: [assistant, saved], liveTools }, expected, `${name} persisted`);
    check({ messages: [assistant, saved], liveTools: started.sessionData.s.liveTools }, expected, `${name} saved result beats stale running event`);
    check({ messages: [assistant, saved] }, expected, `${name} reload`);
    // Orphaned results lack the tool name needed for the no-match exception.
    if (!["rg", "grep", "fd"].includes(name)) check({ messages: [saved] }, expected, `${name} orphan`);
  }
  for (const result of ["plain text", "{invalid", "null", "[]", "", null]) {
    check({ messages: [{ id: "t", type: "tool", callID: "c", result }] }, "completed", "unstructured result remains readable");
  }
});

for (const { name, run } of tests) {
  await run();
  console.log(`ok - ${name}`);
}
console.log(`${tests.length} tool activity tests passed`);
