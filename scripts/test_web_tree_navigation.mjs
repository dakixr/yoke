import assert from "node:assert/strict";
import test from "node:test";
import { TreeDestination } from "../src/yoke/web/assets/js/inspector/tree/destination.js";
import { destinationText, navigationConsequence, nextTreeMatch, treeMatches, treeView, formatTreeTime } from "../src/yoke/web/assets/js/inspector/tree/model.js";
import { defaultTreeEntries, displayTreeEntries, treeGraphLayout } from "../src/yoke/web/assets/js/inspector/tree-graph.js";
import { treeKeyboardAction, treeKeyboardTarget } from "../src/yoke/web/assets/js/inspector/tree-keyboard.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("initial and cleared destinations render without a preview", () => {
  for (const entry of [null, undefined]) {
    assert.doesNotThrow(() => destinationText(entry, [], null));
    assert.doesNotThrow(() => destinationText(entry, [], undefined));
  }
});

function fixture() {
  const requests = [];
  const moves = [];
  let clears = 0;
  let sharedPreview = null;
  let moveResult = { leafID: "a" };
  const destination = new TreeDestination({
    preview: (id) => {
      const request = deferred();
      requests.push({ id, ...request });
      return request.promise;
    },
    clearPreview: () => { clears += 1; sharedPreview = null; },
    navigate: (id, summary) => { moves.push({ id, summary }); return moveResult; },
  });
  return {
    destination, requests, moves,
    tree: { revision: 7, leafID: "head" },
    get clears() { return clears; },
    get sharedPreview() { return sharedPreview; },
    set moveResult(value) { moveResult = value; },
    finish(index, extra = {}) {
      const preview = { targetID: requests[index].id, current: false, abandonedTotal: 2, ...extra };
      sharedPreview = preview;
      requests[index].resolve(preview);
      return preview;
    },
  };
}

test("selection is immediate, latest preview wins without dropping keyboard intent", async () => {
  const f = fixture();
  const a = f.destination.select("a", f.tree);
  const b = f.destination.select("b", f.tree);
  const c = f.destination.select("c", f.tree);
  assert.deepEqual(f.requests.map((request) => request.id), ["a", "b", "c"]);
  assert.equal(f.destination.state.targetID, "c");
  assert.equal(f.destination.state.pending, true);
  await f.destination.continue(f.tree, null);
  assert.equal(f.moves.length, 0);
  const correct = f.finish(2);
  await c;
  assert.equal(f.destination.ready(f.tree, correct), true);
  f.finish(0);
  f.requests[1].reject(new Error("old failure"));
  await Promise.all([a, b]);
  assert.equal(f.destination.state.preview, correct);
  assert.equal(f.destination.state.error, null);
  assert.equal(f.destination.ready(f.tree, correct), true);
});

test("returning to the same target still requires its newest request", async () => {
  const f = fixture();
  const oldA = f.destination.select("a", f.tree);
  const b = f.destination.select("b", f.tree);
  const newA = f.destination.select("a", f.tree);
  const old = f.finish(0);
  await oldA;
  assert.equal(f.destination.ready(f.tree, old), false);
  f.finish(1);
  await b;
  assert.equal(f.destination.state.pending, true);
  const current = f.finish(2);
  await newA;
  assert.equal(f.destination.ready(f.tree, current), true);
});

test("Escape cancels a pending selection and an eventual result cannot rearm it", async () => {
  const f = fixture();
  const selection = f.destination.select("a", f.tree);
  f.destination.clear();
  const result = f.finish(0);
  await selection;
  assert.equal(f.destination.state.targetID, null);
  assert.equal(f.destination.ready(f.tree, result), false);
  assert.equal(f.clears, 2);
});

test("Current selects the anchor, clears an old request, and never moves", async () => {
  const f = fixture();
  const selection = f.destination.select("a", f.tree);
  await f.destination.select("head", f.tree);
  const old = f.finish(0);
  await selection;
  await f.destination.continue(f.tree, old);
  assert.equal(f.destination.state.targetID, "head");
  assert.equal(f.destination.state.pending, false);
  assert.equal(f.destination.state.preview, null);
  assert.equal(f.requests.length, 1);
  assert.equal(f.moves.length, 0);
});

test("commit requires the completed shared preview and the same revision", async () => {
  const f = fixture();
  const selection = f.destination.select("a", f.tree);
  const preview = f.finish(0);
  await selection;
  for (const [tree, shared] of [
    [f.tree, null], [f.tree, { ...preview }], [f.tree, { targetID: "b" }],
    [{ ...f.tree, revision: 8 }, preview], [{ ...f.tree, leafID: "a" }, preview],
  ]) {
    assert.equal(f.destination.ready(tree, shared), false);
    await f.destination.continue(tree, shared);
  }
  assert.equal(f.moves.length, 0);
  await f.destination.continue(f.tree, preview, "keep the finding");
  assert.deepEqual(f.moves, [{ id: "a", summary: "keep the finding" }]);
  assert.equal(f.destination.state.targetID, null);
});

test("repeat Enter cannot commit, and a move is locked synchronously against doubles", async () => {
  const f = fixture();
  const selection = f.destination.select("a", f.tree);
  const preview = f.finish(0);
  await selection;
  await f.destination.continue(f.tree, preview, null, { repeat: true });
  assert.equal(f.moves.length, 0);
  const move = deferred();
  f.moveResult = move.promise;
  const first = f.destination.continue(f.tree, preview);
  const second = f.destination.continue(f.tree, preview);
  await f.destination.select("b", f.tree);
  assert.equal(f.moves.length, 1);
  assert.equal(f.destination.state.targetID, "a");
  move.resolve({ leafID: "a" });
  await Promise.all([first, second]);
  await f.destination.continue(f.tree, preview);
  assert.equal(f.moves.length, 1);
});

test("conflicts and navigation failures disarm the old preview", async () => {
  for (const fail of [false, true]) {
    const f = fixture();
    const selection = f.destination.select("a", f.tree);
    const preview = f.finish(0);
    await selection;
    f.moveResult = fail ? Promise.reject(new Error("offline")) : null;
    await f.destination.continue(f.tree, preview);
    assert.equal(f.destination.state.preview, null);
    assert.ok(f.destination.state.error);
    await f.destination.continue(f.tree, preview);
    assert.equal(f.moves.length, 1);
  }
});

test("missing, mismatched and failed previews are retryable but cannot move", async () => {
  for (const response of [null, { targetID: "wrong" }, new Error("offline")]) {
    const f = fixture();
    const selection = f.destination.select("a", f.tree);
    if (response instanceof Error) f.requests[0].reject(response);
    else f.requests[0].resolve(response);
    await selection;
    assert.equal(f.destination.state.pending, false);
    assert.ok(f.destination.state.error);
    await f.destination.continue(f.tree, response);
    assert.equal(f.moves.length, 0);
    const retry = f.destination.select("a", f.tree);
    const preview = f.finish(1);
    await retry;
    assert.equal(f.destination.ready(f.tree, preview), true);
  }
});

test("disposing a view discards its in-flight response", async () => {
  const f = fixture();
  const selection = f.destination.select("a", f.tree);
  f.destination.dispose();
  const preview = f.finish(0);
  await selection;
  assert.equal(f.destination.state.preview, null);
  assert.equal(f.destination.ready(f.tree, preview), false);
});

test("a closed view cannot commit a completed preview or clear a newer view after a move", async () => {
  const f = fixture();
  const selection = f.destination.select("a", f.tree);
  const preview = f.finish(0);
  await selection;
  const move = deferred();
  f.moveResult = move.promise;
  const moving = f.destination.continue(f.tree, preview);
  const clears = f.clears;
  f.destination.dispose();
  assert.equal(f.destination.ready(f.tree, preview), false);
  move.resolve({ leafID: "a" });
  assert.equal(await moving, null);
  assert.equal(f.clears, clears);
});

const entries = [
  { id: "root", kind: "user", active: true, parentID: "outside", preview: "Find the cache bug", childCount: 2 },
  { id: "call", kind: "assistant_tool_calls", active: true, parentID: "root" },
  { id: "result", kind: "tool_result", active: true, parentID: "call" },
  { id: "answer", kind: "assistant", active: true, parentID: "result", preview: "Cache scope fixed", label: "Working baseline" },
  { id: "head", kind: "checkpoint", active: true, parentID: "answer" },
  { id: "comment", kind: "assistant", phase: "commentary", parentID: "root" },
  { id: "branch", kind: "assistant", parentID: "comment", preview: "Try another cache", label: "Experiment" },
];
const tree = { revision: 3, leafID: "head", entries, totalEntries: 100, cursor: { next: "older" } };

test("Messages retains an exceptional technical Current and counts only what is hidden", () => {
  const view = treeView(tree);
  assert.deepEqual(view.rows.map((entry) => entry.id), ["root", "answer", "head", "branch"]);
  assert.equal(view.rows.find((entry) => entry.id === "head").technicalAnchor, true);
  assert.equal(view.messageCount, 3);
  assert.equal(view.anchorCount, 1);
  assert.equal(view.hiddenCount, 3);
  assert.equal(view.rows.length + view.hiddenCount, entries.length);
  assert.equal(view.latestID, "branch");
  const all = treeView(tree, true);
  assert.equal(all.rows.length, 7);
  assert.equal(all.hiddenCount, 0);
  assert.equal(all.anchorCount, 0);
});

test("hidden technical bridging, forks, external parents and the active lane survive", () => {
  const view = treeView(tree);
  const graph = treeGraphLayout(view.rows);
  assert.equal(view.rows.find((entry) => entry.id === "answer").graphParentID, "root");
  assert.equal(view.rows.find((entry) => entry.id === "branch").graphParentID, "root");
  assert.equal(graph.nodes[0].externalParent, true);
  for (const id of ["root", "answer", "head"]) assert.equal(graph.nodes.find((node) => node.id === id).lane, 0);
  assert.ok(graph.nodes.find((node) => node.id === "branch").lane > 0);
  assert.ok(graph.edges.some((edge) => edge.parentID === "root" && edge.childID === "answer" && edge.active));
  const expanded = treeView({ ...tree, entries: [{ id: "outside", kind: "user", active: true }, ...entries] });
  assert.equal(expanded.rows.find((entry) => entry.id === "root").graphParentID, "outside");
  assert.equal(expanded.rows.find((entry) => entry.id === "root").graphExternalParent, false);
  // The old message-only projection remains available to existing consumers.
  assert.equal(defaultTreeEntries(entries).length, 3);
  assert.equal(displayTreeEntries(entries, defaultTreeEntries(entries))[1].graphParentID, "root");
});

test("Current is authoritative even if an entry flag is stale; empty windows are safe", () => {
  const view = treeView({ ...tree, entries: entries.map((entry) => ({ ...entry, current: entry.id === "branch" })) });
  assert.deepEqual(view.rows.filter((entry) => entry.current).map((entry) => entry.id), ["head"]);
  assert.deepEqual(treeView(null).rows, []);
  assert.equal(treeView({ entries: [], leafID: "outside" }).latestID, null);
});

test("find matches labels, message words and IDs without filtering topology", () => {
  const { rows } = treeView(tree);
  const original = rows.map((entry) => entry.id);
  assert.deepEqual(treeMatches(rows, "BASELINE cache"), ["answer"]);
  assert.deepEqual(treeMatches(rows, "experiment"), ["branch"]);
  assert.deepEqual(treeMatches(rows, "head"), ["head"]);
  assert.deepEqual(treeMatches(rows, "  "), []);
  assert.deepEqual(rows.map((entry) => entry.id), original);
  const matches = treeMatches(rows, "cache");
  assert.equal(nextTreeMatch(matches, "branch"), "root");
  assert.equal(nextTreeMatch(matches, "root", -1), "branch");
  assert.equal(nextTreeMatch(matches, null, -1), "branch");
  assert.equal(nextTreeMatch([], "root"), null);
});

test("ancestor copy never implies that later descendants stay in active context", () => {
  assert.match(navigationConsequence({ active: true }, { abandonedTotal: 0 }), /removes its later active descendants from active context/);
  assert.match(navigationConsequence({ active: false }, { abandonedTotal: 4 }), /4 active nodes leave active context/);
  assert.match(navigationConsequence({ current: true }, null), /already continues here/);
  assert.match(navigationConsequence({}, null), /Checking/);
});

test("times retain calendar context and details include seconds", () => {
  const value = "2026-02-14T12:34:57Z";
  assert.match(formatTreeTime(value, { details: true }), /57/);
  assert.match(formatTreeTime(value, { details: true }), /2026/);
  assert.notEqual(formatTreeTime(value), "Time unavailable");
  assert.equal(formatTreeTime("invalid"), "Time unavailable");
});

test("destination context uses matching full text, never another node's editor preview", () => {
  const entry = { id: "a", preview: "Short snippet..." };
  const messages = [{ id: "a", content: [{ type: "text", text: "Full message" }, { type: "image", url: "hidden" }] }];
  assert.deepEqual(destinationText(entry, messages, { targetID: "b", editorText: "Wrong message" }), { text: "Full message", partial: false });
  assert.deepEqual(destinationText(entry, [], { targetID: "a", editorText: "Restored prompt" }), { text: "Restored prompt", partial: false });
  assert.deepEqual(destinationText(entry, [], null), { text: "Short snippet...", partial: true });
});

test("keyboard actions distinguish selecting, deliberate continuation and repeats", () => {
  assert.equal(treeKeyboardAction({ key: "ArrowDown", repeat: true }), "select");
  assert.equal(treeKeyboardAction({ key: "Enter" }), "continue");
  assert.equal(treeKeyboardAction({ key: "Enter", repeat: true }), "ignore");
  assert.equal(treeKeyboardAction({ key: "Enter", isComposing: true }), null);
  assert.equal(treeKeyboardAction({ key: "ArrowLeft", altKey: true }), null);
  assert.equal(treeKeyboardAction({ key: "Escape" }), "clear");
  assert.equal(treeKeyboardAction({ key: " " }), "choose");
  const { rows } = treeView(tree);
  assert.equal(treeKeyboardTarget(rows, "answer", "ArrowLeft"), "root");
  assert.equal(treeKeyboardTarget(rows, "root", "ArrowRight"), "answer");
  assert.equal(treeKeyboardTarget(rows, "answer", "ArrowDown"), "head");
  assert.equal(treeKeyboardTarget(rows, "root", "ArrowUp"), "root");
  assert.equal(treeKeyboardTarget(rows, "head", "Home"), "root");
  assert.equal(treeKeyboardTarget(rows, "root", "End"), "branch");
  assert.equal(treeKeyboardTarget(rows, "root", "PageDown"), "branch");
  assert.equal(treeKeyboardTarget(rows, "branch", "PageUp"), "root");
  assert.equal(treeKeyboardTarget([], null, "ArrowDown"), null);
});
