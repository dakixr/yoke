import assert from "node:assert/strict";

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

globalThis.localStorage = new MemoryStorage();
globalThis.sessionStorage = new MemoryStorage();
globalThis.window = {
  location: { pathname: "/", search: "", hash: "", href: "http://127.0.0.1/" },
  innerWidth: 1440,
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
  prompt() { return null; },
};
globalThis.location = globalThis.window.location;
globalThis.history = { pushState() {}, replaceState() {} };
globalThis.document = { querySelector() { return null; } };
globalThis.PopStateEvent = class PopStateEvent {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (timer) => clearTimeout(timer);

const { api, ApiError } = await import("../src/yoke/web/assets/js/api/client.js");
const { controller } = await import("../src/yoke/web/assets/js/state/controller.js");
const { reducePublicEvent } = await import("../src/yoke/web/assets/js/state/reducer.js");
const { store } = await import("../src/yoke/web/assets/js/state/store.js");
const { chatActivityForRuntime } = await import("../src/yoke/web/assets/js/session/activity.js");
const { assistantMetadataMessageIDs, compactToolBatchMessageIDs } = await import("../src/yoke/web/assets/js/lib/messages.js");
const { displayTreeEntries, treeGraphLayout } = await import("../src/yoke/web/assets/js/inspector/tree-graph.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolve = resolveValue;
    reject = rejectValue;
  });
  return { promise, resolve, reject };
}

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function sessionSummary(id, overrides = {}) {
  return {
    id,
    title: "Original",
    pinned: false,
    archivedAt: null,
    location: { directory: "/tmp/repo" },
    time: { created: "2026-08-27T00:00:00Z", updated: "2026-08-27T00:00:00Z" },
    selection: { provider: "codex", model: "old-model", reasoningEffort: "medium" },
    tree: { leafID: null, entryCount: 0 },
    queue: { total: 0, steering: 0, queued: 0, paused: 0, revision: 0 },
    ...overrides,
  };
}

function installSession(id, { queue = null, permissions = [], questions = [], extraData = {}, summary = {} } = {}) {
  const session = sessionSummary(id, summary);
  store.setState((state) => ({
    ...state,
    sessions: { ...state.sessions, [id]: session },
    sessionOrder: [id, ...state.sessionOrder.filter((value) => value !== id)],
    sessionData: {
      ...state.sessionData,
      [id]: {
        loaded: true,
        messages: [],
        queue,
        permissions,
        questions,
        ...extraData,
      },
    },
    attention: {
      ...state.attention,
      [id]: { permissions: permissions.length, questions: questions.length },
    },
  }));
  return session;
}

function restoreApi(overrides) {
  const originals = new Map();
  for (const [name, value] of Object.entries(overrides)) {
    originals.set(name, api[name]);
    api[name] = value;
  }
  return () => {
    for (const [name, value] of originals) api[name] = value;
  };
}

async function testPromptAppearsBeforeAdmissionReturns() {
  const id = "optimistic-prompt";
  installSession(id, { queue: { revision: 0, items: [] } });
  const gate = deferred();
  const restore = restoreApi({
    admitPrompt: () => gate.promise,
    queue: async () => ({ data: { revision: 1, items: [] } }),
  });
  try {
    const pending = controller.submitPrompt(id, { text: "instant user row", delivery: "steer" });
    const live = store.getState().sessionData[id].livePrompt;
    assert.equal(live.prompt.text, "instant user row");
    assert.match(live.id, /^inp_/);
    gate.resolve({ data: { id: live.id } });
    await pending;
  } finally {
    restore();
  }
}

async function testPromptRollbackOnFailure() {
  const id = "optimistic-prompt-failure";
  installSession(id, { queue: { revision: 0, items: [] } });
  const gate = deferred();
  const restore = restoreApi({
    admitPrompt: () => gate.promise,
    queue: async () => ({ data: { revision: 0, items: [] } }),
  });
  try {
    const pending = controller.submitPrompt(id, { text: "will fail", delivery: "steer" });
    assert.equal(store.getState().sessionData[id].livePrompt.prompt.text, "will fail");
    gate.reject(new ApiError(500, "fixture_failure", "fixture failure"));
    await assert.rejects(pending, /fixture failure/);
    assert.equal(store.getState().sessionData[id].livePrompt, null);
  } finally {
    restore();
  }
}

async function testSessionPatchesSerializeAndKeepNewestOptimism() {
  const id = "optimistic-session";
  installSession(id);
  const calls = [];
  const restore = restoreApi({
    patchSession: (_id, patch) => {
      const gate = deferred();
      calls.push({ patch, gate });
      return gate.promise;
    },
    getSession: async () => ({ data: sessionSummary(id) }),
  });
  try {
    const first = controller.patchSession(id, { title: "Renamed" });
    const second = controller.patchSession(id, { pinned: true });
    assert.equal(store.getState().sessions[id].title, "Renamed");
    assert.equal(store.getState().sessions[id].pinned, true);
    await tick();
    assert.equal(calls.length, 1, "session mutations should serialize on the wire");
    calls[0].gate.resolve({ data: sessionSummary(id, { title: "Renamed" }) });
    await tick();
    assert.equal(store.getState().sessions[id].pinned, true, "older response must not erase newer optimism");
    assert.equal(calls.length, 2);
    calls[1].gate.resolve({ data: sessionSummary(id, { title: "Renamed", pinned: true }) });
    await Promise.all([first, second]);
    assert.equal(store.getState().sessions[id].title, "Renamed");
    assert.equal(store.getState().sessions[id].pinned, true);
  } finally {
    restore();
  }
}

async function testQueueMutationsSurviveStaleRefresh() {
  const id = "optimistic-queue";
  const base = {
    revision: 10,
    items: [
      { id: "a", prompt: { text: "a", attachments: [] }, delivery: "queue", paused: false, createdAt: "a", state: "admitted" },
      { id: "b", prompt: { text: "b", attachments: [] }, delivery: "queue", paused: false, createdAt: "b", state: "admitted" },
    ],
  };
  installSession(id, { queue: base });
  controller.installQueue(id, base);
  const patchCalls = [];
  const staleRefresh = deferred();
  let queueReads = 0;
  const restore = restoreApi({
    patchQueue: (_id, body) => {
      const gate = deferred();
      patchCalls.push({ body, gate });
      return gate.promise;
    },
    queue: () => {
      queueReads += 1;
      return queueReads === 1 ? staleRefresh.promise : Promise.resolve({ data: base });
    },
  });
  try {
    const refresh = controller.refreshQueue(id);
    const move = controller.patchQueue(id, [{ op: "moveAfter", id: "a", afterID: "b" }]);
    const pause = controller.patchQueue(id, [{ op: "setPaused", id: "a", paused: true }]);
    let visible = store.getState().sessionData[id].queue;
    assert.deepEqual(visible.items.map((item) => item.id), ["b", "a"]);
    assert.equal(visible.items.find((item) => item.id === "a").paused, true);
    await tick();
    assert.equal(patchCalls.length, 1);
    assert.equal(patchCalls[0].body.expectedRevision, 10);

    staleRefresh.resolve({ data: base });
    await refresh;
    visible = store.getState().sessionData[id].queue;
    assert.deepEqual(visible.items.map((item) => item.id), ["b", "a"]);
    assert.equal(visible.items.find((item) => item.id === "a").paused, true);

    patchCalls[0].gate.resolve({
      data: { ...base, revision: 11, items: [base.items[1], base.items[0]] },
    });
    await tick();
    assert.equal(patchCalls.length, 2);
    assert.equal(patchCalls[1].body.expectedRevision, 11);
    assert.equal(store.getState().sessionData[id].queue.items.find((item) => item.id === "a").paused, true);
    patchCalls[1].gate.resolve({
      data: {
        ...base,
        revision: 12,
        items: [base.items[1], { ...base.items[0], paused: true }],
      },
    });
    await Promise.all([move, pause]);
    visible = store.getState().sessionData[id].queue;
    assert.deepEqual(visible.items.map((item) => item.id), ["b", "a"]);
    assert.equal(visible.items[1].paused, true);
    assert.equal(visible.revision, 12);
  } finally {
    restore();
  }
}

async function testHumanInputOldReadCannotResurrectResolvedRequest() {
  const id = "optimistic-human";
  const permission = { id: "perm-1" };
  installSession(id, { permissions: [permission] });
  const oldPermissions = deferred();
  const oldQuestions = deferred();
  const reply = deferred();
  const restore = restoreApi({
    permissions: () => oldPermissions.promise,
    questions: () => oldQuestions.promise,
    replyPermission: () => reply.promise,
  });
  try {
    const staleRead = controller.refreshHumanInput(id);
    const resolving = controller.replyPermission(id, permission.id, "once");
    assert.deepEqual(store.getState().sessionData[id].permissions, []);
    oldPermissions.resolve({ data: [permission] });
    oldQuestions.resolve({ data: [] });
    await staleRead;
    assert.deepEqual(store.getState().sessionData[id].permissions, []);
    reply.resolve({ data: {} });
    await resolving;
  } finally {
    restore();
  }
}

async function testSelectionAndToolTogglesKeepLatestClick() {
  const id = "optimistic-config";
  installSession(id, {
    extraData: {
      tools: [{ name: "alpha", enabled: true }],
      contextUsage: { input_tokens: 50_000, max_total_tokens: 100_000, usage_percent: 50 },
    },
  });
  const selectionCalls = [];
  const toolCalls = [];
  const restore = restoreApi({
    selectModel: (_id, body) => {
      const gate = deferred();
      selectionCalls.push({ body, gate });
      return gate.promise;
    },
    patchTools: (_id, body) => {
      const gate = deferred();
      toolCalls.push({ body, gate });
      return gate.promise;
    },
    tools: async () => ({ data: [{ name: "alpha", enabled: true }] }),
  });
  try {
    const low = controller.setSelection(id, "codex", "new-model", "low");
    const high = controller.setSelection(id, "codex", "new-model", "high");
    assert.equal(store.getState().sessions[id].selection.reasoningEffort, "high");
    assert.equal(store.getState().sessionData[id].contextUsage, null);
    await tick();
    assert.equal(selectionCalls.length, 1);
    selectionCalls[0].gate.resolve({ data: { effective: selectionCalls[0].body } });
    await tick();
    assert.equal(store.getState().sessions[id].selection.reasoningEffort, "high");
    assert.equal(selectionCalls.length, 2);
    selectionCalls[1].gate.resolve({ data: { effective: selectionCalls[1].body } });
    await Promise.all([low, high]);
    assert.equal(store.getState().sessions[id].selection.reasoningEffort, "high");

    const off = controller.toggleTool(id, "alpha", false);
    const on = controller.toggleTool(id, "alpha", true);
    assert.equal(store.getState().sessionData[id].tools[0].enabled, true);
    await tick();
    assert.equal(toolCalls.length, 1);
    toolCalls[0].gate.resolve({ data: { enabled: [] } });
    await tick();
    assert.equal(store.getState().sessionData[id].tools[0].enabled, true);
    assert.equal(toolCalls.length, 2);
    toolCalls[1].gate.resolve({ data: { enabled: ["alpha"] } });
    await Promise.all([off, on]);
    assert.equal(store.getState().sessionData[id].tools[0].enabled, true);
  } finally {
    restore();
  }
}

async function testDraftSendPaintsSessionAndPromptBeforeAdmissionReturns() {
  const draftID = "draft-optimistic-send";
  store.setState((state) => ({
    ...state,
    drafts: {
      ...state.drafts,
      [draftID]: {
        id: draftID,
        text: "start immediately",
        location: "/tmp/repo",
        provider: "codex",
        model: "gpt-test",
        reasoningEffort: "medium",
        attachments: [],
      },
    },
  }));
  const admission = deferred();
  let createdID = null;
  const restore = restoreApi({
    createSession: async (body) => {
      createdID = body.id;
      return {
        data: sessionSummary(body.id, {
          title: body.title,
          location: body.location,
          selection: body.selection,
        }),
      };
    },
    admitPrompt: () => admission.promise,
    queue: async () => ({ data: { revision: 0, items: [] } }),
  });
  try {
    const pending = controller.submitDraft(draftID);
    await tick();
    assert.ok(createdID);
    const state = store.getState();
    assert.equal(state.ui.selectedSessionID, createdID);
    assert.equal(state.sessions[createdID].title, "start immediately");
    assert.equal(state.sessionData[createdID].livePrompt.prompt.text, "start immediately");
    assert.ok(state.drafts[draftID], "draft should survive until admission succeeds");
    admission.resolve({ data: { id: state.sessionData[createdID].livePrompt.id } });
    await pending;
    assert.equal(store.getState().drafts[draftID], undefined);
  } finally {
    restore();
  }
}

async function testCompactionShowsImmediatelyAndRollsBackFailure() {
  const id = "optimistic-compaction";
  installSession(id);
  const gate = deferred();
  const restore = restoreApi({ compact: () => gate.promise });
  try {
    const pending = controller.compact(id);
    const active = store.getState().active[id];
    assert.equal(active.state, "running");
    assert.equal(active.activity, "Compacting");
    gate.reject(new ApiError(500, "fixture_failure", "fixture failure"));
    await assert.rejects(pending, /fixture failure/);
    assert.equal(store.getState().active[id], undefined);
  } finally {
    restore();
  }
}

async function testTreeLabelsSerializeAndKeepNewestOptimism() {
  const id = "optimistic-tree-label";
  installSession(id, {
    extraData: {
      tree: {
        revision: 10,
        leafID: "node-1",
        totalEntries: 1,
        cursor: { next: null },
        entries: [{
          id: "node-1",
          parentID: null,
          kind: "assistant",
          createdAt: "2026-08-27T00:00:00Z",
          label: null,
          active: true,
          current: true,
          preview: "node",
          childCount: 0,
        }],
      },
    },
  });
  const calls = [];
  const restore = restoreApi({
    patchTreeEntry: (_sessionID, entryID, body) => {
      const gate = deferred();
      calls.push({ entryID, body, gate });
      return gate.promise;
    },
  });
  try {
    const first = controller.labelTreeEntry(id, "node-1", "First");
    const second = controller.labelTreeEntry(id, "node-1", "Second");
    assert.equal(store.getState().sessionData[id].tree.entries[0].label, "Second");
    await tick();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].body.expectedRevision, 10);
    calls[0].gate.resolve({
      data: {
        revision: 11,
        entry: { ...store.getState().sessionData[id].tree.entries[0], label: "First" },
      },
    });
    await tick();
    assert.equal(store.getState().sessionData[id].tree.entries[0].label, "Second");
    assert.equal(calls.length, 2);
    assert.equal(calls[1].body.expectedRevision, 11);
    calls[1].gate.resolve({
      data: {
        revision: 12,
        entry: { ...store.getState().sessionData[id].tree.entries[0], label: "Second" },
      },
    });
    await Promise.all([first, second]);
    assert.equal(store.getState().sessionData[id].tree.entries[0].label, "Second");
    assert.equal(store.getState().sessionData[id].tree.revision, 12);
  } finally {
    restore();
  }
}

function testLateDurableEventDoesNotClearNewerOptimisticPrompt() {
  const id = "optimistic-reducer";
  const base = {
    connection: {},
    sessions: {},
    sessionOrder: [],
    archivedOrder: [],
    active: {},
    attention: {},
    ui: { doneUnreviewed: {}, selectedSessionID: id },
    sessionData: {
      [id]: {
        messages: [],
        livePrompt: {
          id: "new-input",
          prompt: { text: "new", attachments: [] },
          timeCreated: "2026-08-27T12:00:02Z",
        },
      },
    },
  };
  const reduced = reducePublicEvent(base, {
    id: "event",
    type: "session.message.updated",
    time: "2026-08-27T12:00:03Z",
    sessionID: id,
    durable: { seq: 2 },
    data: { inputID: "old-input" },
  });
  assert.equal(reduced.sessionData[id].livePrompt.id, "new-input");
}

async function testCheckpointedToolCommentaryDoesNotStickAtTail() {
  const id = "checkpointed-commentary";
  installSession(id);
  store.setState((state) => ({
    ...state,
    active: {
      ...state.active,
      [id]: {
        state: "running",
        turnID: "turn-1",
        startedAt: "2026-08-27T12:00:00Z",
        activity: "Running tool",
      },
    },
  }));
  store.setState((state) => reducePublicEvent(state, {
    id: "live-commentary",
    type: "session.message.updated",
    time: "2026-08-27T12:00:01Z",
    sessionID: id,
    data: {
      turnID: "turn-1",
      iteration: 1,
      phase: "commentary",
      content: "I will inspect the file first.",
    },
  }));
  assert.equal(Object.keys(store.getState().sessionData[id].liveAssistants).length, 1);

  const restore = restoreApi({
    messages: async () => ({
      data: [{
        id: "assistant-tool-call",
        type: "assistant",
        timeCreated: "2026-08-27T12:00:01.100Z",
        phase: null,
        content: [{ type: "text", text: "I will inspect the file first." }],
        toolCalls: [{ id: "call-1", name: "read_file", arguments: '{"path":"README.md"}' }],
      }],
      snapshotSeq: 1,
      cursor: { next: null },
    }),
  });
  try {
    await controller.refreshMessages(id);
    assert.deepEqual(
      store.getState().sessionData[id].liveAssistants,
      {},
      "checkpointed tool commentary must reconcile into its persisted assistant row",
    );
  } finally {
    restore();
  }
}

async function testBootstrapDoesNotWaitForProviderOrLocationEnrichment() {
  const providerGate = deferred();
  const locationGate = deferred();
  const processGate = deferred();
  const restore = restoreApi({
    capabilities: async () => ({ data: { limits: {} } }),
    activeSessions: async () => ({ data: {} }),
    commands: async () => ({ data: [] }),
    recentLocations: async () => ({ data: [] }),
    listSessions: async () => ({ data: [], cursor: { next: null } }),
    providers: () => providerGate.promise,
  });
  const originals = {
    startSse: controller.startSse,
    resolveVisibleLocations: controller.resolveVisibleLocations,
    refreshProcessLocalState: controller.refreshProcessLocalState,
    applyRoute: controller.applyRoute,
  };
  controller.startSse = () => {};
  controller.resolveVisibleLocations = () => locationGate.promise;
  controller.refreshProcessLocalState = () => processGate.promise;
  controller.applyRoute = async () => {};
  try {
    const completed = controller.bootstrap().then(() => true);
    assert.equal(
      await Promise.race([completed, delay(100).then(() => false)]),
      true,
      "bootstrap should not wait for provider or location enrichment",
    );
    assert.deepEqual(store.getState().providers, []);
    providerGate.resolve({ data: [{ id: "codex", ready: true }] });
    await tick();
    assert.equal(store.getState().providers[0].id, "codex");
  } finally {
    locationGate.resolve();
    processGate.resolve();
    Object.assign(controller, originals);
    restore();
  }
}

async function testChatWorkingIndicatorTracksRuntimeState() {
  assert.equal(
    chatActivityForRuntime({ state: "running", activity: "Thinking" }),
    "Thinking",
  );
  assert.equal(
    chatActivityForRuntime({ state: "running", activity: null }),
    "Working",
    "running state must keep an in-chat indicator even if an activity event is temporarily absent",
  );
  assert.equal(
    chatActivityForRuntime({ state: "stopping", activity: null }),
    "Stopping",
  );
  assert.equal(
    chatActivityForRuntime({ state: "idle", activity: "Thinking" }),
    null,
    "stale activity must not keep the chat indicator after the runtime is idle",
  );
}

async function testAssistantMetadataOnlyMarksFinalTextPerTurn() {
  const ids = assistantMetadataMessageIDs([
    { id: "u1", type: "user", content: [{ type: "text", text: "first" }] },
    { id: "a1", type: "assistant", content: [{ type: "text", text: "commentary one" }] },
    { id: "a2", type: "assistant", content: [], toolCalls: [{ id: "tool-1" }] },
    { id: "a3", type: "assistant", content: [{ type: "text", text: "final one" }] },
    { id: "u2", type: "user", content: [{ type: "text", text: "second" }] },
    { id: "a4", type: "assistant", content: [{ type: "text", text: "commentary two" }] },
    { id: "a5", type: "assistant", content: [{ type: "image", name: "result.png" }] },
    { id: "a6", type: "assistant", content: [{ type: "text", text: "final two" }] },
  ]);
  assert.deepEqual([...ids], ["a3", "a6"]);
}

async function testSequentialToolOnlyBatchesCompactWithoutEatingTextSpacing() {
  const ids = compactToolBatchMessageIDs([
    { id: "u1", type: "user", content: [{ type: "text", text: "go" }] },
    { id: "a1", type: "assistant", content: [], toolCalls: [{ id: "t1" }, { id: "t2" }] },
    { id: "r1", type: "tool", callID: "t1" },
    { id: "r2", type: "tool", callID: "t2" },
    { id: "a2", type: "assistant", content: [], toolCalls: [{ id: "t3" }] },
    { id: "r3", type: "tool", callID: "t3" },
    { id: "a3", type: "assistant", content: [], toolCalls: [{ id: "t4" }] },
    { id: "a4", type: "assistant", content: [{ type: "text", text: "done" }] },
    { id: "u2", type: "user", content: [{ type: "text", text: "next" }] },
    { id: "a5", type: "assistant", content: [], toolCalls: [{ id: "t5" }] },
  ]);
  assert.deepEqual([...ids], ["a1", "a2"]);
}

async function testSelectionEventClearsRecoveredContextUsage() {
  const id = "context-selection-reset";
  installSession(id, {
    extraData: {
      contextUsage: { input_tokens: 80_000, max_total_tokens: 100_000, usage_percent: 80 },
    },
  });
  const next = reducePublicEvent(store.getState(), {
    type: "session.selection.changed",
    sessionID: id,
    data: { provider: "codex", model: "new-model" },
    durable: { seq: 12 },
  });
  assert.equal(next.sessionData[id].contextUsage, null);
}

async function testTreeGraphKeepsCurrentLaneAndSeparatesOverlappingBranches() {
  const entries = [
    { id: "root", parentID: null, kind: "user", active: true },
    { id: "shared", parentID: "root", kind: "assistant", active: true },
    { id: "main-user", parentID: "shared", kind: "user", active: true },
    { id: "main-leaf", parentID: "main-user", kind: "assistant", active: true, current: true },
    { id: "branch-a-user", parentID: "shared", kind: "user", active: false },
    { id: "branch-a-leaf", parentID: "branch-a-user", kind: "assistant", active: false },
    { id: "branch-b-user", parentID: "root", kind: "user", active: false },
    { id: "branch-b-leaf", parentID: "branch-b-user", kind: "assistant", active: false },
  ];
  const graph = treeGraphLayout(displayTreeEntries(entries, entries));
  const lanes = Object.fromEntries(graph.nodes.map((node) => [node.id, node.lane]));
  assert.equal(lanes.root, 0);
  assert.equal(lanes["main-leaf"], 0);
  assert.notEqual(lanes["branch-a-leaf"], 0);
  assert.notEqual(lanes["branch-b-leaf"], 0);
  assert.notEqual(
    lanes["branch-a-leaf"],
    lanes["branch-b-leaf"],
    "branches whose connector spans overlap must not share a graph lane",
  );
}

async function testTreeGraphBridgesHiddenTechnicalNodes() {
  const entries = [
    { id: "user", parentID: null, kind: "user", active: true },
    { id: "tool-call", parentID: "user", kind: "assistant_tool_calls", active: true },
    { id: "tool-result", parentID: "tool-call", kind: "tool", active: true },
    { id: "assistant", parentID: "tool-result", kind: "assistant", active: true, current: true },
  ];
  const visible = displayTreeEntries(entries, [entries[0], entries[3]]);
  assert.equal(visible[1].graphParentID, "user");
  const graph = treeGraphLayout(visible);
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0].parentID, "user");
  assert.equal(graph.edges[0].childID, "assistant");
}

const tests = [
  testPromptAppearsBeforeAdmissionReturns,
  testPromptRollbackOnFailure,
  testSessionPatchesSerializeAndKeepNewestOptimism,
  testQueueMutationsSurviveStaleRefresh,
  testHumanInputOldReadCannotResurrectResolvedRequest,
  testSelectionAndToolTogglesKeepLatestClick,
  testDraftSendPaintsSessionAndPromptBeforeAdmissionReturns,
  testCompactionShowsImmediatelyAndRollsBackFailure,
  testTreeLabelsSerializeAndKeepNewestOptimism,
  testLateDurableEventDoesNotClearNewerOptimisticPrompt,
  testCheckpointedToolCommentaryDoesNotStickAtTail,
  testBootstrapDoesNotWaitForProviderOrLocationEnrichment,
  testChatWorkingIndicatorTracksRuntimeState,
  testAssistantMetadataOnlyMarksFinalTextPerTurn,
  testSequentialToolOnlyBatchesCompactWithoutEatingTextSpacing,
  testSelectionEventClearsRecoveredContextUsage,
  testTreeGraphKeepsCurrentLaneAndSeparatesOverlappingBranches,
  testTreeGraphBridgesHiddenTechnicalNodes,
];

const started = performance.now();
for (const test of tests) {
  store.reset();
  await test();
  console.log(`PASS ${test.name}`);
}
console.log(JSON.stringify({ tests: tests.length, milliseconds: performance.now() - started }));
