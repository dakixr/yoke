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
const { installActiveSnapshot, mergeSessionInfo, reducePublicEvent } = await import("../src/yoke/web/assets/js/state/reducer.js");
const { store } = await import("../src/yoke/web/assets/js/state/store.js");
const { chatActivityForRuntime } = await import("../src/yoke/web/assets/js/session/activity.js");
const { assistantMetadataMessageIDs, compactToolBatchMessageIDs } = await import("../src/yoke/web/assets/js/lib/messages.js");
const { defaultTreeEntries, displayTreeEntries, treeGraphLayout } = await import("../src/yoke/web/assets/js/inspector/tree-graph.js");
const { sortToolCallsChronologically } = await import("../src/yoke/web/assets/js/inspector/tool-logic.js");
const {
  connectionStatusDescriptor,
  hasPendingQueue,
  queueStatusLabel,
  sessionStatusDescriptor,
} = await import("../src/yoke/web/assets/js/components/sidebar-status.js");
const { treeKeyboardTarget } = await import("../src/yoke/web/assets/js/inspector/tree-keyboard.js");
const { createLocationBrowseCoordinator, isLocationBrowseQuery } = await import("../src/yoke/web/assets/js/session/location-picker-logic.js");
const { filterModelChoices, groupModelChoices, modelNavigationIndex, modelSelectionErrorMessage, resolveModelEffort } = await import("../src/yoke/web/assets/js/session/model-picker-logic.js");
const { slashMenuScrollDelta } = await import("../src/yoke/web/assets/js/session/slash-menu-logic.js");
const { formatTurnSummary } = await import("../src/yoke/web/assets/js/session/turn-summary.js");
const { installKeybindings } = await import("../src/yoke/web/assets/js/lib/keyboard.js");
const { visualSessionOrder } = await import("../src/yoke/web/assets/js/state/session-order.js");
const { readSessionComposerDrafts } = await import("../src/yoke/web/assets/js/state/local-state.js");
const {
  clearSessionComposerDraft,
  getSessionComposerDraft,
  updateSessionComposerDraft,
} = await import("../src/yoke/web/assets/js/state/session-composer-drafts.js");

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
        messageSnapshotLoaded: true,
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

async function testSessionComposerDraftsStayScopedAndPersisted() {
  const firstID = "composer-draft-first";
  const secondID = "composer-draft-second";
  const image = {
    id: "upl_draft",
    uri: "yoke-upload://upl_draft",
    name: "sketch.png",
    mime: "image/png",
    size: 123,
  };
  updateSessionComposerDraft(firstID, {
    text: "half-written prompt",
    attachments: [image],
  });
  updateSessionComposerDraft(secondID, { text: "another session draft" });

  assert.equal(getSessionComposerDraft(firstID).text, "half-written prompt");
  assert.deepEqual(getSessionComposerDraft(firstID).attachments, [image]);
  assert.equal(getSessionComposerDraft(secondID).text, "another session draft");

  const persisted = readSessionComposerDrafts();
  assert.equal(persisted[firstID].text, "half-written prompt");
  assert.deepEqual(persisted[firstID].attachments, [image]);
  assert.equal(persisted[secondID].text, "another session draft");

  clearSessionComposerDraft(firstID);
  assert.equal(getSessionComposerDraft(firstID).text, "");
  assert.equal(readSessionComposerDrafts()[firstID], undefined);
  clearSessionComposerDraft(secondID);
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
  installSession("optimistic-prompt-older");
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
    assert.equal(store.getState().sessionOrder[0], id);
    assert.equal(store.getState().sessions[id].time.lastUserMessage, live.timeCreated);
    gate.resolve({ data: { id: live.id } });
    await pending;
  } finally {
    restore();
  }
}

async function testFailedTurnDoesNotDelayNextOptimisticPrompt() {
  const id = "optimistic-prompt-after-failure";
  installSession(id, {
    queue: { revision: 0, items: [] },
    extraData: {
      lastError: "provider failed",
      livePrompt: {
        id: "inp_failed",
        prompt: { text: "failed prompt", attachments: [] },
        delivery: "steer",
        timeCreated: "2026-08-27T12:00:00Z",
      },
    },
  });
  const admission = deferred();
  let admitBody = null;
  let messageReads = 0;
  const restore = restoreApi({
    admitPrompt: (_sessionID, body) => {
      admitBody = body;
      return admission.promise;
    },
    messages: async () => {
      messageReads += 1;
      return { data: [], cursor: { next: null }, snapshotSeq: 0 };
    },
    queue: async () => ({ data: { revision: 0, items: [] } }),
  });
  try {
    const pending = controller.submitPrompt(id, { text: "new prompt", delivery: "steer" });
    const data = store.getState().sessionData[id];
    assert.equal(data.livePrompt.prompt.text, "new prompt");
    assert.equal(data.failedPrompts.length, 1);
    assert.equal(data.failedPrompts[0].prompt.text, "failed prompt");
    assert.equal(admitBody.prompt.text, "new prompt");
    assert.equal(messageReads, 0, "next optimistic prompt must not wait for history recovery");
    admission.resolve({ data: { id: data.livePrompt.id } });
    await pending;
  } finally {
    restore();
  }
}

async function testDurableMessageKeepsOptimisticPromptUntilSnapshot() {
  const id = "optimistic-prompt-durable-handoff";
  const inputID = "inp_handoff";
  installSession(id, {
    extraData: {
      livePrompt: {
        id: inputID,
        prompt: { text: "stay visible", attachments: [] },
        delivery: "steer",
        timeCreated: "2026-08-27T12:00:00Z",
      },
    },
  });
  store.setState((state) => reducePublicEvent(state, {
    id: "durable-handoff",
    type: "session.message.updated",
    time: "2026-08-27T12:00:01Z",
    sessionID: id,
    durable: { seq: 2 },
    data: { inputID },
  }));
  assert.equal(
    store.getState().sessionData[id].livePrompt.id,
    inputID,
    "terminal event must not create an empty gap before the persisted row arrives",
  );

  const persisted = {
    id: "msg_user_handoff",
    type: "user",
    inputID,
    timeCreated: "2026-08-27T12:00:00Z",
    content: [{ type: "text", text: "stay visible" }],
  };
  const restore = restoreApi({
    messages: async () => ({
      data: [persisted],
      cursor: { next: null },
      snapshotSeq: 2,
    }),
  });
  try {
    await controller.refreshMessages(id);
    assert.equal(store.getState().sessionData[id].livePrompt, null);
    assert.equal(store.getState().sessionData[id].messages.at(-1).inputID, inputID);
  } finally {
    restore();
  }
}

async function testPromptRollbackOnFailure() {
  const id = "optimistic-prompt-failure";
  const previous = installSession(id, { queue: { revision: 0, items: [] } });
  const otherID = "optimistic-prompt-failure-other";
  installSession(otherID);
  const previousOrder = [...store.getState().sessionOrder];
  const gate = deferred();
  const restore = restoreApi({
    admitPrompt: () => gate.promise,
    queue: async () => ({ data: { revision: 0, items: [] } }),
  });
  try {
    const pending = controller.submitPrompt(id, { text: "will fail", delivery: "steer" });
    assert.equal(store.getState().sessionData[id].livePrompt.prompt.text, "will fail");
    assert.equal(store.getState().sessionOrder[0], id);
    gate.reject(new ApiError(500, "fixture_failure", "fixture failure"));
    await assert.rejects(pending, /fixture failure/);
    assert.equal(store.getState().sessionData[id].livePrompt, null);
    assert.deepEqual(store.getState().sessionOrder, previousOrder);
    assert.deepEqual(store.getState().sessions[id].time, previous.time);
  } finally {
    restore();
  }
}

async function testSettledPromptReopensOptimistically() {
  const id = "settled-prompt-reopen";
  const archivedAt = "2026-09-01T12:00:00Z";
  installSession(id, {
    queue: { revision: 0, items: [] },
    summary: { archivedAt },
  });
  store.setState((state) => ({
    ...state,
    sessionOrder: state.sessionOrder.filter((value) => value !== id),
    archivedOrder: [id, ...state.archivedOrder.filter((value) => value !== id)],
  }));
  const gate = deferred();
  const restore = restoreApi({
    admitPrompt: () => gate.promise,
    queue: async () => ({ data: { revision: 1, items: [] } }),
  });
  try {
    const pending = controller.submitPrompt(id, { text: "continue this", delivery: "steer" });
    const state = store.getState();
    assert.equal(state.sessions[id].archivedAt, null);
    assert.equal(state.sessionOrder[0], id);
    assert.equal(state.archivedOrder.includes(id), false);
    assert.equal(state.sessionData[id].livePrompt.prompt.text, "continue this");
    gate.resolve({ data: { id: state.sessionData[id].livePrompt.id } });
    await pending;
  } finally {
    restore();
  }
}

async function testSettledPromptFailureRestoresSettledState() {
  const id = "settled-prompt-reopen-failure";
  const archivedAt = "2026-09-01T12:00:00Z";
  installSession(id, {
    queue: { revision: 0, items: [] },
    summary: { archivedAt },
  });
  store.setState((state) => ({
    ...state,
    sessionOrder: state.sessionOrder.filter((value) => value !== id),
    archivedOrder: [id, ...state.archivedOrder.filter((value) => value !== id)],
  }));
  const restore = restoreApi({
    admitPrompt: async () => { throw new ApiError(500, "fixture_failure", "fixture failure"); },
    queue: async () => ({ data: { revision: 0, items: [] } }),
  });
  try {
    await assert.rejects(
      controller.submitPrompt(id, { text: "continue this", delivery: "steer" }),
      /fixture failure/,
    );
    const state = store.getState();
    assert.equal(state.sessions[id].archivedAt, archivedAt);
    assert.equal(state.sessionOrder.includes(id), false);
    assert.equal(state.archivedOrder[0], id);
    assert.equal(state.sessionData[id].livePrompt, null);
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

async function testPinningPreservesSessionRecencyOrder() {
  const ids = ["pin-order-a", "pin-order-b", "pin-order-c"];
  for (const id of ids) installSession(id);
  store.setState((state) => ({ ...state, sessionOrder: [...ids] }));
  const gate = deferred();
  const restore = restoreApi({
    patchSession: () => gate.promise,
  });
  try {
    const pending = controller.patchSession(ids[1], { pinned: true });
    assert.deepEqual(store.getState().sessionOrder, ids);
    assert.deepEqual(
      visualSessionOrder(store.getState().sessionOrder, store.getState().sessions),
      [ids[1], ids[0], ids[2]],
    );
    gate.resolve({ data: sessionSummary(ids[1], { pinned: true }) });
    await pending;
    assert.deepEqual(store.getState().sessionOrder, ids);
  } finally {
    restore();
  }
}

async function testSessionShortcutFollowsPinnedVisualOrder() {
  const ids = ["switch-a", "switch-b", "switch-c", "switch-d"];
  for (const id of ids) installSession(id);
  store.setState((state) => ({
    ...state,
    sessions: {
      ...state.sessions,
      [ids[1]]: { ...state.sessions[ids[1]], pinned: true },
      [ids[3]]: { ...state.sessions[ids[3]], pinned: true },
    },
    sessionOrder: [...ids],
    ui: { ...state.ui, selectedSessionID: ids[1] },
  }));
  assert.deepEqual(
    visualSessionOrder(store.getState().sessionOrder, store.getState().sessions),
    [ids[1], ids[3], ids[0], ids[2]],
  );
  controller.switchSession(1);
  assert.equal(store.getState().ui.selectedSessionID, ids[3]);
  controller.switchSession(1);
  assert.equal(store.getState().ui.selectedSessionID, ids[0]);
  controller.switchSession(-1);
  assert.equal(store.getState().ui.selectedSessionID, ids[3]);
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

async function testServerRestartLetsEmptyQueueReplaceStaleUi() {
  const id = "queue-revision-reset";
  const stale = {
    revision: 7,
    items: [
      { id: "sent", prompt: { text: "already sent", attachments: [] }, delivery: "queue", paused: false, createdAt: "a", state: "admitted" },
    ],
  };
  installSession(id, { queue: stale });
  controller.installQueue(id, stale);
  controller.bufferEvents = false;
  store.setState((state) => ({
    ...state,
    connection: { ...state.connection, current: true, serverInstanceID: "old-server" },
  }));
  const originalResync = controller.resync;
  controller.resync = async () => {};
  const restore = restoreApi({
    queue: async () => ({ data: { revision: 0, items: [] } }),
    listSessions: async ({ archived }) => archived
      ? { data: [], total: 0, cursor: { next: null } }
      : {
          data: [sessionSummary(id, {
            queue: { total: 0, steering: 0, queued: 0, paused: 0, revision: 0 },
          })],
          total: 1,
          cursor: { next: null },
        },
  });
  try {
    await controller.refreshQueue(id);
    assert.equal(
      store.getState().sessionData[id].queue.items.length,
      1,
      "a lower revision from the same server must not erase newer queue state",
    );

    controller.receiveEvent({
      type: "server.connected",
      data: { serverInstanceID: "new-server" },
      durable: null,
    });
    await controller.refreshSessionLists();
    assert.deepEqual(store.getState().sessions[id].queue, {
      total: 0,
      steering: 0,
      queued: 0,
      paused: 0,
      revision: 0,
    });
    await controller.refreshQueue(id);
    assert.deepEqual(store.getState().sessionData[id].queue, { revision: 0, items: [] });
  } finally {
    controller.resync = originalResync;
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

async function testProcessRefreshDoesNotRestoreAnOldSelection() {
  const id = "process-refresh-selection";
  installSession(id, {
    extraData: {
      processes: [],
      processDetail: { processID: "proc-old", sessionID: id },
    },
  });
  const gate = deferred();
  let listArgs = null;
  const restore = restoreApi({
    process: () => gate.promise,
    processes: async (args) => {
      listArgs = args;
      return { data: [] };
    },
  });
  try {
    const refreshing = controller.refreshProcess("proc-old");
    controller.setSessionField(id, "processDetail", {
      processID: "proc-new",
      sessionID: id,
    });
    gate.resolve({ data: { processID: "proc-old", sessionID: id } });
    await refreshing;
    assert.equal(store.getState().sessionData[id].processDetail.processID, "proc-new");

    await controller.refreshProcesses(id);
    assert.equal(listArgs.limit, 200);
  } finally {
    restore();
  }
}

async function testLoadOlderMessagesSkipsDuplicateOnlyPages() {
  const id = "message-pagination-duplicates";
  const message = (messageID) => ({
    id: messageID,
    type: "assistant",
    content: [{ type: "text", text: messageID }],
  });
  installSession(id, {
    extraData: {
      messages: [message("m2"), message("m1")],
      messageCursor: "cursor-1",
    },
  });
  const cursors = [];
  const restore = restoreApi({
    messages: async (_sessionID, options) => {
      cursors.push(options.cursor || null);
      if (options.cursor === "cursor-1") {
        return { data: [message("m2")], cursor: { next: "cursor-2" } };
      }
      assert.equal(options.cursor, "cursor-2");
      return { data: [message("m3")], cursor: { next: null } };
    },
  });
  try {
    const result = await controller.loadOlderMessages(id);
    assert.deepEqual(cursors, ["cursor-1", "cursor-2"]);
    assert.equal(result.addedCount, 1);
    assert.equal(result.skippedDuplicatePages, 1);
    assert.deepEqual(
      store.getState().sessionData[id].messages.map((item) => item.id),
      ["m3", "m2", "m1"],
    );
    assert.equal(store.getState().sessionData[id].messageCursor, null);
    assert.equal(store.getState().sessionData[id].loadingOlder, false);
  } finally {
    restore();
  }
}

async function testLoadOlderMessagesRecoversInvalidCursor() {
  const id = "message-pagination-stale-cursor";
  const message = (messageID) => ({
    id: messageID,
    type: "assistant",
    content: [{ type: "text", text: messageID }],
  });
  installSession(id, {
    extraData: {
      messages: [message("stale-old"), message("stale-latest")],
      messageCursor: "stale-cursor",
    },
  });
  const cursors = [];
  const restore = restoreApi({
    messages: async (_sessionID, options) => {
      cursors.push(options.cursor || null);
      if (options.cursor === "stale-cursor") {
        throw new ApiError(400, "invalid_cursor_anchor", "Cursor anchor no longer exists.");
      }
      if (!options.cursor) {
        return {
          data: [message("fresh-latest"), message("fresh-middle")],
          cursor: { next: "fresh-cursor" },
        };
      }
      assert.equal(options.cursor, "fresh-cursor");
      return { data: [message("fresh-old")], cursor: { next: null } };
    },
  });
  try {
    const result = await controller.loadOlderMessages(id);
    assert.deepEqual(cursors, ["stale-cursor", null, "fresh-cursor"]);
    assert.equal(result.addedCount, 1);
    assert.equal(result.recoveredCursor, true);
    assert.deepEqual(
      store.getState().sessionData[id].messages.map((item) => item.id),
      ["fresh-old", "fresh-middle", "fresh-latest"],
    );
    assert.equal(store.getState().sessionData[id].messageCursor, null);
    assert.equal(store.getState().sessionData[id].loadingOlder, false);
  } finally {
    restore();
  }
}

async function testProcessOutputRefreshAppendsIncrementallyWithoutDuplication() {
  const id = "process-output-stream";
  installSession(id, {
    extraData: {
      processes: [],
      processDetail: {
        processID: "proc-live",
        sessionID: id,
        status: "running",
        output: { tail: "one\n", latestSeq: 1, retainedBytes: 4 },
      },
    },
  });
  const first = deferred();
  const second = deferred();
  let outputCalls = 0;
  const restore = restoreApi({
    processOutput: (_processID, afterSeq) => {
      outputCalls += 1;
      assert.equal(afterSeq, outputCalls === 1 ? 1 : 2);
      return outputCalls === 1 ? first.promise : second.promise;
    },
  });
  try {
    const refreshA = controller.refreshProcessOutput("proc-live");
    const refreshB = controller.refreshProcessOutput("proc-live");
    assert.equal(outputCalls, 1);
    first.resolve({
      data: [{ seq: 2, stream: "combined", text: "two\n" }],
      cursor: { next: 2, truncatedBefore: 0 },
    });
    await Promise.all([refreshA, refreshB]);
    let detail = store.getState().sessionData[id].processDetail;
    assert.equal(detail.output.tail, "one\ntwo\n");
    assert.equal(detail.output.latestSeq, 2);

    const refreshC = controller.refreshProcessOutput("proc-live");
    controller.setSessionField(id, "processDetail", {
      ...detail,
      output: { ...detail.output, tail: "one\ntwo\nthree\n", latestSeq: 3 },
    });
    second.resolve({
      data: [{ seq: 3, stream: "combined", text: "three\n" }],
      cursor: { next: 3, truncatedBefore: 0 },
    });
    await refreshC;
    detail = store.getState().sessionData[id].processDetail;
    assert.equal(detail.output.tail, "one\ntwo\nthree\n");
    assert.equal(detail.output.latestSeq, 3);
  } finally {
    restore();
  }
}

async function testProcessMetadataRefreshCannotMoveOutputBackward() {
  const id = "process-output-metadata-race";
  installSession(id, {
    extraData: {
      processDetail: {
        processID: "proc-live",
        sessionID: id,
        status: "running",
        elapsedMs: 100,
        output: { tail: "one\ntwo\n", latestSeq: 2, retainedBytes: 8 },
      },
    },
  });
  const restore = restoreApi({
    process: async () => ({
      data: {
        processID: "proc-live",
        sessionID: id,
        status: "running",
        elapsedMs: 200,
        output: { tail: "one\n", latestSeq: 1, retainedBytes: 4 },
      },
    }),
  });
  try {
    await controller.refreshProcess("proc-live");
    const detail = store.getState().sessionData[id].processDetail;
    assert.equal(detail.elapsedMs, 200);
    assert.equal(detail.output.tail, "one\ntwo\n");
    assert.equal(detail.output.latestSeq, 2);
  } finally {
    restore();
  }
}

async function testToolDetailKeepsNewestSelection() {
  const id = "tool-detail-selection";
  installSession(id, { extraData: { toolDetail: null } });
  const oldGate = deferred();
  const newGate = deferred();
  const restore = restoreApi({
    toolCall: (_sessionID, callID) => callID === "call-old" ? oldGate.promise : newGate.promise,
    toolOutput: async () => ({ data: [], cursor: { next: 0, truncatedBefore: 0 } }),
  });
  try {
    const oldLoad = controller.loadToolCall(id, "call-old");
    const newLoad = controller.loadToolCall(id, "call-new");
    newGate.resolve({ data: { id: "call-new", toolName: "new", status: "ok" } });
    await tick();
    oldGate.resolve({ data: { id: "call-old", toolName: "old", status: "ok" } });
    await Promise.all([oldLoad, newLoad]);
    assert.equal(store.getState().sessionData[id].toolDetail.id, "call-new");
  } finally {
    restore();
  }
}

async function testPersistedToolDetailSkipsOutputRequest() {
  const id = "tool-persisted-detail";
  installSession(id, {
    extraData: {
      toolCalls: [{ id: "call-session", toolName: "read", status: "ok", retention: "session" }],
      toolDetail: null,
    },
  });
  let outputReads = 0;
  const restore = restoreApi({
    toolCall: async () => ({
      data: { id: "call-session", toolName: "read", status: "ok", retention: "session" },
    }),
    toolOutput: async () => {
      outputReads += 1;
      return { data: [], cursor: { next: 0, truncatedBefore: 0 } };
    },
  });
  try {
    await controller.loadToolCall(id, "call-session");
    assert.equal(outputReads, 0);
    assert.deepEqual(store.getState().sessionData[id].toolDetail.outputChunks, []);
  } finally {
    restore();
  }
}

async function testToolDetailCoalescesSameCallRequest() {
  const id = "tool-detail-coalescing";
  installSession(id, {
    extraData: {
      toolCalls: [{ id: "call-live", toolName: "exec", status: "running", retention: "runtime" }],
      toolDetail: null,
    },
  });
  const detailGate = deferred();
  const outputGate = deferred();
  let detailReads = 0;
  let outputReads = 0;
  const restore = restoreApi({
    toolCall: async () => {
      detailReads += 1;
      return detailGate.promise;
    },
    toolOutput: async () => {
      outputReads += 1;
      return outputGate.promise;
    },
  });
  try {
    const first = controller.loadToolCall(id, "call-live");
    const second = controller.loadToolCall(id, "call-live");
    assert.equal(detailReads, 1);
    assert.equal(outputReads, 1);
    detailGate.resolve({
      data: { id: "call-live", toolName: "exec", status: "running", retention: "runtime" },
    });
    outputGate.resolve({ data: [{ seq: 1, stream: "output", text: "hi" }], cursor: { next: 1, truncatedBefore: 0 } });
    await Promise.all([first, second]);
    assert.equal(store.getState().sessionData[id].toolDetail.id, "call-live");
    assert.equal(store.getState().sessionData[id].toolDetail.outputChunks.length, 1);
  } finally {
    restore();
  }
}

async function testToolTimelineDeepLinkDoesNotLockInspectorSelection() {
  const id = "tool-deep-link-selection";
  installSession(id, { extraData: { toolDetail: null } });
  store.setState((state) => ({
    ...state,
    ui: {
      ...state.ui,
      selectedSessionID: id,
      inspector: { mode: "tool", callID: "call-from-chat" },
    },
  }));
  const oldGate = deferred();
  const newGate = deferred();
  const restore = restoreApi({
    toolCall: (_sessionID, callID) => callID === "call-from-chat" ? oldGate.promise : newGate.promise,
    toolOutput: async (_sessionID, callID) => ({
      data: [{ text: `${callID} output` }],
      cursor: { next: 0, truncatedBefore: 0 },
    }),
  });
  try {
    const initial = controller.loadToolCall(id, "call-from-chat");
    const selected = controller.selectToolCall(id, "call-other");
    assert.equal(store.getState().ui.inspector.callID, "call-other");

    newGate.resolve({ data: { id: "call-other", toolName: "other", status: "ok" } });
    await selected;
    oldGate.resolve({ data: { id: "call-from-chat", toolName: "original", status: "ok" } });
    await initial;

    assert.equal(store.getState().sessionData[id].toolDetail.id, "call-other");
    assert.equal(store.getState().ui.inspector.callID, "call-other");
  } finally {
    restore();
  }
}

async function testTimelineToolOpenKeepsNewestClickAcrossListRace() {
  const id = "tool-open-click-race";
  installSession(id, { extraData: { toolDetail: null, toolCalls: null } });
  store.setState((state) => ({
    ...state,
    ui: { ...state.ui, selectedSessionID: id },
  }));
  const firstList = deferred();
  const oldDetail = deferred();
  const newDetail = deferred();
  let listReads = 0;
  const restore = restoreApi({
    toolCalls: async () => {
      listReads += 1;
      if (listReads === 1) return firstList.promise;
      return { data: [{ id: "call-new", toolName: "new", status: "ok" }], cursor: { next: null } };
    },
    toolCall: (_sessionID, callID) => callID === "call-old" ? oldDetail.promise : newDetail.promise,
    toolOutput: async () => ({ data: [], cursor: { next: 0, truncatedBefore: 0 } }),
  });
  try {
    const oldOpen = controller.openInspector("tool", { callID: "call-old" });
    const newOpen = controller.openInspector("tool", { callID: "call-new" });

    newDetail.resolve({ data: { id: "call-new", toolName: "new", status: "ok" } });
    await tick();
    firstList.resolve({ data: [{ id: "call-old", toolName: "old", status: "ok" }], cursor: { next: null } });
    oldDetail.resolve({ data: { id: "call-old", toolName: "old", status: "ok" } });
    await Promise.all([oldOpen, newOpen]);

    assert.equal(store.getState().ui.inspector.callID, "call-new");
    assert.equal(store.getState().sessionData[id].toolDetail.id, "call-new");
  } finally {
    restore();
  }
}

async function testTimelineToolOpenLoadsCallOutsideSidebarPage() {
  const id = "tool-open-outside-sidebar";
  installSession(id, { extraData: { toolDetail: null, toolCalls: null } });
  store.setState((state) => ({
    ...state,
    ui: { ...state.ui, selectedSessionID: id },
  }));
  const restore = restoreApi({
    toolCalls: async () => ({
      data: [{ id: "call-newest", toolName: "newest", status: "ok" }],
      cursor: { next: "older-page" },
    }),
    toolCall: async (_sessionID, callID) => ({
      data: { id: callID, toolName: "historical", status: "ok" },
    }),
    toolOutput: async () => ({ data: [], cursor: { next: 0, truncatedBefore: 0 } }),
  });
  try {
    await controller.openInspector("tool", { callID: "call-from-old-history" });

    assert.deepEqual(store.getState().sessionData[id].toolCalls.map((call) => call.id), ["call-newest"]);
    assert.equal(store.getState().ui.inspector.callID, "call-from-old-history");
    assert.equal(store.getState().sessionData[id].toolDetail.id, "call-from-old-history");
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
    assert.equal(state.sessions[createdID].title, null);
    assert.equal(state.sessionData[createdID].messageSnapshotLoaded, false);
    assert.equal(state.sessionData[createdID].livePrompt.prompt.text, "start immediately");
    assert.ok(state.drafts[draftID], "draft should survive until admission succeeds");
    admission.resolve({ data: { id: state.sessionData[createdID].livePrompt.id } });
    await pending;
    assert.equal(store.getState().drafts[draftID], undefined);
  } finally {
    restore();
  }
}

async function testBackgroundDraftSubmitKeepsUserOnFreshDraft() {
  const draftID = "draft-background-send";
  store.setState((state) => ({
    ...state,
    ui: { ...state.ui, selectedSessionID: null, newSession: true },
    drafts: {
      ...state.drafts,
      [draftID]: {
        id: draftID,
        text: "run this in background",
        location: "/tmp/background-repo",
        provider: "zai",
        model: "glm-test",
        reasoningEffort: "high",
        attachments: [],
      },
    },
  }));
  const admission = deferred();
  let createdID = null;
  const restore = restoreApi({
    createSession: async (body) => {
      createdID = body.id;
      return { data: sessionSummary(body.id, { location: body.location, selection: body.selection }) };
    },
    admitPrompt: () => admission.promise,
    queue: async () => ({ data: { revision: 0, items: [] } }),
  });
  try {
    const pending = controller.submitDraft(draftID, { background: true });
    await tick();
    assert.ok(createdID);
    assert.equal(store.getState().ui.selectedSessionID, null);
    admission.resolve({ data: { id: store.getState().sessionData[createdID].livePrompt.id } });
    await pending;
    const state = store.getState();
    assert.equal(state.ui.selectedSessionID, null);
    assert.equal(state.drafts[draftID], undefined);
    const fresh = Object.values(state.drafts).find((draft) => draft.id !== draftID && draft.location === "/tmp/background-repo");
    assert.ok(fresh);
    assert.equal(fresh.text, "");
    assert.equal(fresh.provider, "zai");
    assert.equal(fresh.model, "glm-test");
    assert.equal(fresh.reasoningEffort, "high");
  } finally {
    restore();
  }
}

async function testTurnSummaryMatchesCliFormatting() {
  assert.equal(formatTurnSummary({ durationSeconds: 59.9, toolCount: 4 }), "");
  assert.equal(formatTurnSummary({ durationSeconds: 60, toolCount: 0 }), "Worked for 1m00s");
  assert.equal(formatTurnSummary({ durationSeconds: 83.9, toolCount: 1 }), "Worked for 1m23s · 1 tool");
  assert.equal(formatTurnSummary({ durationSeconds: 3_661, toolCount: 7 }), "Worked for 1h01m · 7 tools");
}

async function testLoadSessionHydratesOptimisticEmptySession() {
  const id = "optimistic-unhydrated-session";
  installSession(id, {
    extraData: {
      messageSnapshotLoaded: false,
      loadError: "stale refresh failure",
    },
  });
  let messageCalls = 0;
  const restore = restoreApi({
    messages: async () => {
      messageCalls += 1;
      return {
        data: [{
          id: "persisted-user",
          type: "user",
          content: [{ type: "text", text: "persisted history" }],
          timeCreated: "2026-08-29T00:00:00Z",
        }],
        cursor: { previous: null, next: null },
        snapshotSeq: 4,
      };
    },
    queue: async () => ({ data: { revision: 0, items: [] } }),
    permissions: async () => ({ data: [] }),
    questions: async () => ({ data: [] }),
    history: async () => ({ data: [], hasMore: false }),
  });
  try {
    await controller.loadSession(id);
    const data = store.getState().sessionData[id];
    assert.equal(messageCalls, 1);
    assert.equal(data.messageSnapshotLoaded, true);
    assert.equal(data.loadError, null);
    assert.equal(data.messages[0].id, "persisted-user");
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
    { id: "a0", type: "assistant", content: [{ type: "text", text: "I will check." }], toolCalls: [{ id: "t0" }] },
    { id: "r0", type: "tool", callID: "t0" },
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
  assert.deepEqual([...ids], ["a0", "a1", "a2"]);
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
  assert.ok(graph.edges.some((edge) => edge.childID === "main-leaf" && edge.active));
  assert.ok(graph.edges.some((edge) => edge.childID === "branch-a-leaf" && !edge.active));
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

async function testTreeDefaultViewKeepsUsersAndFinalAssistants() {
  const entries = [
    { id: "user-1", parentID: null, kind: "user" },
    { id: "commentary", parentID: "user-1", kind: "assistant", phase: "commentary" },
    { id: "tool-call", parentID: "commentary", kind: "assistant_tool_calls" },
    { id: "tool-result", parentID: "tool-call", kind: "tool" },
    { id: "final", parentID: "tool-result", kind: "assistant", phase: "final_answer" },
    { id: "user-2", parentID: "final", kind: "user" },
    { id: "legacy-final", parentID: "user-2", kind: "assistant", phase: null },
  ];
  assert.deepEqual(
    defaultTreeEntries(entries).map((entry) => entry.id),
    ["user-1", "final", "user-2", "legacy-final"],
  );
  const visible = displayTreeEntries(entries, defaultTreeEntries(entries));
  assert.equal(visible[1].graphParentID, "user-1");
}

async function testLocationBrowseKeepsNewestNavigation() {
  const coordinator = createLocationBrowseCoordinator();
  const first = deferred();
  const second = deferred();
  const commits = [];
  const firstRun = coordinator.run(() => first.promise, (value) => commits.push(value), () => {});
  const secondRun = coordinator.run(() => second.promise, (value) => commits.push(value), () => {});
  second.resolve("second");
  assert.equal(await secondRun, true);
  first.resolve("first");
  assert.equal(await firstRun, false);
  assert.deepEqual(commits, ["second"]);
  assert.equal(isLocationBrowseQuery("~/dev/yo"), true);
  assert.equal(isLocationBrowseQuery("/home/dakixr/dev"), true);
  assert.equal(isLocationBrowseQuery("project-name"), false);
}

async function testCombinedModelPickerFiltersAcrossProviders() {
  const providers = [
    { id: "codex", ready: true },
    { id: "zai", ready: false },
  ];
  const models = [
    { provider: "codex", id: "gpt-5", name: "GPT 5", reasoningEfforts: ["low", "high"] },
    { provider: "codex", id: "gpt-5-mini", name: "GPT 5 Mini", reasoningEfforts: ["low"] },
    { provider: "zai", id: "glm", name: "GLM", reasoningEfforts: ["medium"] },
  ];
  const filtered = filterModelChoices(models, "gpt", providers);
  assert.deepEqual(filtered.map((item) => item.id), ["gpt-5", "gpt-5-mini"]);
  assert.deepEqual(groupModelChoices(filterModelChoices(models, "", providers)).map((group) => group.provider), ["codex", "zai"]);
  assert.equal(filterModelChoices(models, "glm", providers)[0].providerReady, false);
  assert.equal(resolveModelEffort(models[0], "high", "low"), "high");
  assert.equal(resolveModelEffort(models[1], "high", "low"), "low");
  assert.equal(modelNavigationIndex(0, 8, "ArrowUp"), 7);
  assert.equal(modelNavigationIndex(7, 8, "ArrowDown"), 0);
  assert.equal(modelNavigationIndex(3, 8, "Home"), 0);
  assert.equal(modelNavigationIndex(3, 8, "End"), 7);
  assert.equal(modelNavigationIndex(1, 8, "PageDown"), 6);
  assert.equal(modelNavigationIndex(6, 8, "PageUp"), 1);
}

async function testSlashMenuKeepsKeyboardSelectionInsideViewport() {
  assert.equal(slashMenuScrollDelta({ viewportTop: 100, viewportBottom: 300, itemTop: 140, itemBottom: 182 }), 0);
  assert.equal(slashMenuScrollDelta({ viewportTop: 100, viewportBottom: 300, itemTop: 58, itemBottom: 100 }), -42);
  assert.equal(slashMenuScrollDelta({ viewportTop: 100, viewportBottom: 300, itemTop: 300, itemBottom: 342 }), 42);
}

async function testTreeKeyboardNavigationFollowsVisibleTopology() {
  const rows = [
    { id: "root", graphParentID: null, active: true },
    { id: "branch", graphParentID: "root", active: false },
    { id: "current", graphParentID: "root", active: true, current: true },
    { id: "tail", graphParentID: "current", active: true },
  ];
  assert.equal(treeKeyboardTarget(rows, "current", "ArrowUp"), "branch");
  assert.equal(treeKeyboardTarget(rows, "current", "ArrowDown"), "tail");
  assert.equal(treeKeyboardTarget(rows, "current", "ArrowLeft"), "root");
  assert.equal(treeKeyboardTarget(rows, "root", "ArrowRight"), "current");
  assert.equal(treeKeyboardTarget(rows, "branch", "Home"), "root");
  assert.equal(treeKeyboardTarget(rows, "root", "End"), "tail");
  assert.equal(treeKeyboardTarget(rows, "tail", "PageUp", { pageSize: 2 }), "branch");
  assert.equal(treeKeyboardTarget(rows, "root", "PageDown", { pageSize: 2 }), "current");
}

async function testNewSessionGlobalShortcutSupportsMacAndWindows() {
  let handler = null;
  const originalAdd = document.addEventListener;
  const originalRemove = document.removeEventListener;
  document.addEventListener = (type, callback) => {
    if (type === "keydown") handler = callback;
  };
  document.removeEventListener = (type, callback) => {
    if (type === "keydown" && handler === callback) handler = null;
  };
  let created = 0;
  const uninstall = installKeybindings({ newSession: () => { created += 1; } });
  const fire = (overrides) => {
    let prevented = false;
    handler?.({
      key: "o",
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      preventDefault() { prevented = true; },
      ...overrides,
    });
    return prevented;
  };
  try {
    assert.equal(fire({ metaKey: true, shiftKey: true }), true);
    assert.equal(created, 1);
    assert.equal(fire({ ctrlKey: true, shiftKey: true }), true);
    assert.equal(created, 2);
    assert.equal(fire({ metaKey: true }), false);
    assert.equal(fire({ key: "n", metaKey: true }), false);
    assert.equal(fire({ key: "n", ctrlKey: true }), false);
    assert.equal(fire({ ctrlKey: true, shiftKey: true, altKey: true }), false);
    assert.equal(created, 2);
  } finally {
    uninstall();
    document.addEventListener = originalAdd;
    document.removeEventListener = originalRemove;
  }
}

async function testSidebarGlobalShortcutSupportsMacAndWindows() {
  let handler = null;
  const originalAdd = document.addEventListener;
  const originalRemove = document.removeEventListener;
  document.addEventListener = (type, callback) => {
    if (type === "keydown") handler = callback;
  };
  document.removeEventListener = (type, callback) => {
    if (type === "keydown" && handler === callback) handler = null;
  };
  let toggled = 0;
  const uninstall = installKeybindings({ toggleSidebar: () => { toggled += 1; } });
  const fire = (overrides) => {
    let prevented = false;
    handler?.({
      key: "b",
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      preventDefault() { prevented = true; },
      ...overrides,
    });
    return prevented;
  };
  try {
    assert.equal(fire({ metaKey: true }), true);
    assert.equal(toggled, 1);
    assert.equal(fire({ ctrlKey: true }), true);
    assert.equal(toggled, 2);
    assert.equal(fire({ metaKey: true, shiftKey: true }), false);
    assert.equal(fire({ ctrlKey: true, altKey: true }), false);
    assert.equal(toggled, 2);
  } finally {
    uninstall();
    document.addEventListener = originalAdd;
    document.removeEventListener = originalRemove;
  }
}

async function testRemovedCtrlXChordFallsThrough() {
  let handler = null;
  const originalAdd = document.addEventListener;
  const originalRemove = document.removeEventListener;
  document.addEventListener = (type, callback) => {
    if (type === "keydown") handler = callback;
  };
  document.removeEventListener = (type, callback) => {
    if (type === "keydown" && handler === callback) handler = null;
  };
  let opened = 0;
  const uninstall = installKeybindings({ processInspector: () => { opened += 1; } });
  const fire = (overrides) => {
    let prevented = false;
    handler?.({
      key: "",
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      preventDefault() { prevented = true; },
      ...overrides,
    });
    return prevented;
  };
  try {
    assert.equal(fire({ key: "x", ctrlKey: true }), false);
    assert.equal(fire({ key: "p" }), false);
    assert.equal(fire({ key: "p", ctrlKey: true }), false);
    assert.equal(opened, 0);
  } finally {
    uninstall();
    document.addEventListener = originalAdd;
    document.removeEventListener = originalRemove;
  }
}

async function testSettledTotalDoesNotDependOnLoadedPage() {
  const archivedPageOne = Array.from({ length: 30 }, (_, index) =>
    sessionSummary(`archived-${index}`, { archivedAt: "2026-08-29T00:00:00Z" }));
  const archivedPageTwo = Array.from({ length: 30 }, (_, index) =>
    sessionSummary(`archived-${index + 30}`, { archivedAt: "2026-08-28T00:00:00Z" }));
  let archivedReads = 0;
  const restore = restoreApi({
    listSessions: async ({ archived, cursor }) => {
      if (!archived) return { data: [], total: 0, cursor: { next: null } };
      archivedReads += 1;
      if (!cursor) return { data: archivedPageOne, total: 73, cursor: { next: "page-2" } };
      return { data: archivedPageTwo, total: 73, cursor: { next: "page-3" } };
    },
  });
  try {
    await controller.refreshSessionLists();
    assert.equal(store.getState().archivedOrder.length, 30);
    assert.equal(store.getState().archivedTotal, 73);
    await controller.loadMoreSessions(true);
    assert.equal(store.getState().archivedOrder.length, 60);
    assert.equal(store.getState().archivedTotal, 73);
    assert.equal(archivedReads, 2);
  } finally {
    restore();
  }
}

async function testDoneTracksCompletedTurnsOnly() {
  const id = "done-state";
  installSession(id);
  store.setState((state) => ({
    ...state,
    active: { ...state.active, [id]: { state: "running", turnID: 1 } },
    ui: { ...state.ui, selectedSessionID: null, doneUnreviewed: { [id]: false } },
  }));

  let next = reducePublicEvent(store.getState(), {
    type: "session.active.changed",
    sessionID: id,
    data: { state: "idle" },
  });
  assert.equal(next.ui.doneUnreviewed[id], false, "idle alone must not mean an unseen completion");

  next = reducePublicEvent(next, {
    type: "session.message.updated",
    sessionID: id,
    data: { inputID: "inp-done", turnID: 1, status: "completed" },
    durable: { seq: 1 },
  });
  assert.equal(next.ui.doneUnreviewed[id], true);

  next = reducePublicEvent(next, {
    type: "session.active.changed",
    sessionID: id,
    data: { state: "running", turnID: 2, startedAt: "2026-09-01T12:00:00Z" },
  });
  assert.equal(next.ui.doneUnreviewed[id], false, "new work must clear an older Done badge");

  next = {
    ...next,
    ui: { ...next.ui, doneUnreviewed: { ...next.ui.doneUnreviewed, [id]: true } },
  };
  next = installActiveSnapshot(next, {
    [id]: { state: "running", turnID: 2, startedAt: "2026-09-01T12:00:00Z" },
  });
  assert.equal(next.ui.doneUnreviewed[id], false, "reload must clear persisted Done for active work");
}

async function testResyncReplaysCompletionForPreviouslyActiveSession() {
  const id = "resync-completed-background";
  const summary = sessionSummary(id);
  store.setState((state) => ({
    ...state,
    sessions: { ...state.sessions, [id]: summary },
    sessionOrder: [id],
    active: { [id]: { state: "running", turnID: 1 } },
    sessionData: {},
    ui: { ...state.ui, selectedSessionID: null, doneUnreviewed: { [id]: false } },
    connection: { ...state.connection, current: true, status: "connected" },
  }));
  let historyReads = 0;
  const restore = restoreApi({
    activeSessions: async () => ({ data: {} }),
    listSessions: async ({ archived }) => archived
      ? { data: [], total: 0, cursor: { next: null } }
      : { data: [summary], total: 1, cursor: { next: null } },
    history: async (sessionID) => {
      assert.equal(sessionID, id);
      historyReads += 1;
      return {
        data: [{
          type: "session.message.updated",
          sessionID: id,
          data: { inputID: "inp-finished", turnID: 1, status: "completed" },
          durable: { seq: 1 },
        }],
        hasMore: false,
      };
    },
  });
  const originalResolveLocations = controller.resolveVisibleLocations;
  controller.resolveVisibleLocations = async () => {};
  try {
    await controller.resync(false);
    assert.equal(historyReads, 1);
    assert.equal(store.getState().ui.doneUnreviewed[id], true);
  } finally {
    controller.resolveVisibleLocations = originalResolveLocations;
    restore();
  }
}

async function testSidebarStatusPrioritizesCurrentWork() {
  assert.deepEqual(
    sessionStatusDescriptor({
      runtime: { state: "running" },
      attention: null,
      done: true,
      queue: { total: 2, steering: 0, queued: 1, paused: 1 },
      age: "2m",
    }),
    { kind: "working", label: "Working" },
  );
  assert.deepEqual(
    sessionStatusDescriptor({
      runtime: { state: "idle" },
      attention: null,
      done: true,
      queue: { total: 2, steering: 0, queued: 1, paused: 1 },
      age: "2m",
    }),
    { kind: "quiet", label: "1 queued · 1 paused" },
  );
  assert.equal(queueStatusLabel({ steering: 1, queued: 0, paused: 2 }), "1 steer · 2 paused");
  assert.equal(hasPendingQueue({ total: 1 }), true);
  assert.equal(hasPendingQueue({ total: 0 }), false);
}

async function testSessionSummaryKeepsNewestQueueRevision() {
  const id = "queue-summary-race";
  const current = sessionSummary(id, {
    queue: { total: 0, steering: 0, queued: 0, paused: 0, revision: 6 },
  });
  const stale = sessionSummary(id, {
    title: "Fresh title, stale queue",
    queue: { total: 1, steering: 0, queued: 1, paused: 0, revision: 5 },
  });
  const merged = mergeSessionInfo(current, stale, { preserveQueue: true });
  assert.equal(merged.title, "Fresh title, stale queue");
  assert.deepEqual(merged.queue, current.queue);

  const newer = sessionSummary(id, {
    queue: { total: 1, steering: 1, queued: 0, paused: 0, revision: 7 },
  });
  assert.deepEqual(mergeSessionInfo(current, newer, { preserveQueue: true }).queue, newer.queue);

  controller.queueServerRevisions.delete(id);
  installSession(id);
  const restore = restoreApi({
    listSessions: async ({ archived }) => archived
      ? { data: [], total: 0, cursor: { next: null } }
      : { data: [current], total: 1, cursor: { next: null } },
    getSession: async () => ({ data: stale }),
  });
  const originalResolveLocations = controller.resolveVisibleLocations;
  controller.resolveVisibleLocations = async () => {};
  try {
    await controller.refreshSessionLists();
    assert.equal(controller.queueServerRevisions.get(id), 6);
    await controller.refreshSessionSummary(id);
    assert.equal(store.getState().sessions[id].title, "Fresh title, stale queue");
    assert.deepEqual(store.getState().sessions[id].queue, current.queue);
  } finally {
    controller.resolveVisibleLocations = originalResolveLocations;
    restore();
  }
}

async function testQueueSummarySeparatesPausedWork() {
  const id = "paused-queue-summary";
  installSession(id);
  controller.installQueue(id, {
    revision: 4,
    items: [
      { id: "steer-paused", delivery: "steer", paused: true },
      { id: "queue-paused", delivery: "queue", paused: true },
      { id: "queue-ready", delivery: "queue", paused: false },
    ],
  });
  assert.deepEqual(store.getState().sessions[id].queue, {
    total: 3,
    steering: 0,
    queued: 1,
    paused: 2,
    revision: 4,
  });
}

async function testSummaryGuardKeepsAuthoritativeQueueRevision() {
  const id = "queue-summary-optimistic-revision";
  installSession(id);
  controller.queueServerRevisions.set(id, 10);
  controller.installQueue(id, {
    revision: 11,
    items: [{ id: "local-edit", delivery: "queue", paused: false }],
  }, { authoritative: false });
  const restore = restoreApi({
    getSession: async () => ({
      data: sessionSummary(id, {
        title: "Summary response",
        queue: { total: 0, steering: 0, queued: 0, paused: 0, revision: 10 },
      }),
    }),
  });
  try {
    await controller.refreshSessionSummary(id);
    assert.equal(controller.queueServerRevisions.get(id), 10);
    assert.equal(store.getState().sessions[id].queue.revision, 11);
  } finally {
    restore();
  }
}

async function testConnectionIndicatorTracksStreamState() {
  assert.deepEqual(
    connectionStatusDescriptor({ current: true, status: "connected" }),
    { kind: "connected", label: "Connected" },
  );
  assert.deepEqual(
    connectionStatusDescriptor({ current: false, status: "resyncing" }),
    { kind: "syncing", label: "Synchronizing" },
  );
  assert.deepEqual(
    connectionStatusDescriptor({ current: false, status: "connected" }),
    { kind: "syncing", label: "Synchronizing" },
  );
  assert.deepEqual(
    connectionStatusDescriptor({ current: false, status: "disconnected" }),
    { kind: "disconnected", label: "Disconnected" },
  );
}

async function testToolCallsSortChronologically() {
  const calls = [
    { id: "new", time: { started: "2026-09-01T10:00:03Z" } },
    { id: "same-b", time: { started: "2026-09-01T10:00:02Z" } },
    { id: "old", time: { started: "2026-09-01T10:00:01Z" } },
    { id: "same-a", time: { started: "2026-09-01T10:00:02Z" } },
  ];
  assert.deepEqual(
    sortToolCallsChronologically(calls).map((call) => call.id),
    ["old", "same-a", "same-b", "new"],
  );
}

async function testModelSelectionContextErrorStaysSpecific() {
  const contextError = new ApiError(
    409,
    "model_context_too_small",
    "Cannot switch to small-model. The current model was not changed.",
  );
  assert.equal(
    modelSelectionErrorMessage(contextError),
    contextError.message,
  );
  assert.equal(
    modelSelectionErrorMessage(new ApiError(500, "fixture", "Server failed.")),
    "Could not change the model. Server failed.",
  );
}

const tests = [
  testSessionComposerDraftsStayScopedAndPersisted,
  testPromptAppearsBeforeAdmissionReturns,
  testFailedTurnDoesNotDelayNextOptimisticPrompt,
  testDurableMessageKeepsOptimisticPromptUntilSnapshot,
  testPromptRollbackOnFailure,
  testSettledPromptReopensOptimistically,
  testSettledPromptFailureRestoresSettledState,
  testSessionPatchesSerializeAndKeepNewestOptimism,
  testPinningPreservesSessionRecencyOrder,
  testSessionShortcutFollowsPinnedVisualOrder,
  testQueueMutationsSurviveStaleRefresh,
  testServerRestartLetsEmptyQueueReplaceStaleUi,
  testHumanInputOldReadCannotResurrectResolvedRequest,
  testSelectionAndToolTogglesKeepLatestClick,
  testProcessRefreshDoesNotRestoreAnOldSelection,
  testLoadOlderMessagesSkipsDuplicateOnlyPages,
  testLoadOlderMessagesRecoversInvalidCursor,
  testProcessOutputRefreshAppendsIncrementallyWithoutDuplication,
  testProcessMetadataRefreshCannotMoveOutputBackward,
  testToolDetailKeepsNewestSelection,
  testPersistedToolDetailSkipsOutputRequest,
  testToolDetailCoalescesSameCallRequest,
  testToolTimelineDeepLinkDoesNotLockInspectorSelection,
  testTimelineToolOpenKeepsNewestClickAcrossListRace,
  testTimelineToolOpenLoadsCallOutsideSidebarPage,
  testDraftSendPaintsSessionAndPromptBeforeAdmissionReturns,
  testBackgroundDraftSubmitKeepsUserOnFreshDraft,
  testLoadSessionHydratesOptimisticEmptySession,
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
  testTreeDefaultViewKeepsUsersAndFinalAssistants,
  testLocationBrowseKeepsNewestNavigation,
  testCombinedModelPickerFiltersAcrossProviders,
  testSlashMenuKeepsKeyboardSelectionInsideViewport,
  testTreeKeyboardNavigationFollowsVisibleTopology,
  testNewSessionGlobalShortcutSupportsMacAndWindows,
  testSidebarGlobalShortcutSupportsMacAndWindows,
  testRemovedCtrlXChordFallsThrough,
  testSettledTotalDoesNotDependOnLoadedPage,
  testDoneTracksCompletedTurnsOnly,
  testResyncReplaysCompletionForPreviouslyActiveSession,
  testSidebarStatusPrioritizesCurrentWork,
  testSessionSummaryKeepsNewestQueueRevision,
  testQueueSummarySeparatesPausedWork,
  testSummaryGuardKeepsAuthoritativeQueueRevision,
  testConnectionIndicatorTracksStreamState,
  testToolCallsSortChronologically,
  testModelSelectionContextErrorStaysSpecific,
  testTurnSummaryMatchesCliFormatting,
];

const started = performance.now();
for (const test of tests) {
  store.reset();
  await test();
  console.log(`PASS ${test.name}`);
}
console.log(JSON.stringify({ tests: tests.length, milliseconds: performance.now() - started }));
