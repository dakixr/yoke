// @ts-check

// The daemon owns phase transitions. Clients accept complete state values,
// reject old revisions and request a snapshot when an event was missed.
const terminal = new Set(["settled", "removed"]);
const queued = new Set(["queued", "paused"]);
const visible = new Set(["submitting", "promoted", "running"]);

export function isBusy(runtime) {
  return Boolean(runtime && !["idle", "error"].includes(runtime.state));
}

function input(data, previous = {}) {
  const prompt = typeof data.prompt === "string" ? { ...(previous.prompt || {}), text: data.prompt, attachments: previous.prompt?.attachments || [] } : data.prompt;
  return { ...previous, ...data, id: data.inputID || data.id, prompt: prompt || previous.prompt || { text: "", attachments: [] } };
}

function revision(data, value) {
  const current = data.inputRevision || 0;
  if (value <= current) return null;
  return { inputRevision: value, resyncPending: Boolean(data.resyncPending || value !== current + 1) };
}

export function applyInputChanged(data, event) {
  const version = revision(data, event.inputRevision);
  if (!version) return data;
  const inputs = { ...(data.inputs || {}) };
  const id = event.inputID;
  if (terminal.has(event.phase)) delete inputs[id];
  else inputs[id] = input(event, inputs[id]);
  return { ...data, ...version, inputLifecycle: true, inputs };
}

export function applyQueueHeld(data, event) {
  const version = revision(data, event.inputRevision);
  return version ? { ...data, ...version, inputLifecycle: true, held: event.held ?? null } : data;
}

export function applyActiveChanged(data, event) {
  if (Number.isFinite(event.revision) && event.revision <= (data.runtime?.revision ?? -1)) return data;
  return { ...data, runtime: { ...event, error: event.error ?? event.lastError ?? event.last_error ?? null } };
}

export function installInputSnapshot(data, snapshot) {
  if (snapshot.inputRevision < (data.inputRevision || 0)) return data;
  const inputs = Object.fromEntries(Object.entries(data.inputs || {}).filter(([, item]) => item.phase === "submitting"));
  for (const item of snapshot.inputs || []) {
    if (!terminal.has(item.phase)) inputs[item.id] = input(item, data.inputs?.[item.id]);
    else delete inputs[item.id];
  }
  return { ...data, inputs, inputLifecycle: true, inputRevision: snapshot.inputRevision, held: snapshot.held ?? null, resyncPending: false };
}

export function submittingInput(data, item) {
  return { ...data, inputs: { ...(data.inputs || {}), [item.id]: { ...item, phase: "submitting" } } };
}

export function admitSubmission(data, receipt) {
  const current = data.inputs?.[receipt.id];
  if (current?.phase !== "submitting" || !receipt.phase) return data;
  const inputs = { ...data.inputs };
  if (terminal.has(receipt.phase)) delete inputs[receipt.id];
  else inputs[receipt.id] = input(receipt, current);
  return { ...data, inputs };
}

export function rejectSubmission(data, id) {
  if (data.inputs?.[id]?.phase !== "submitting") return data;
  const inputs = { ...data.inputs };
  delete inputs[id];
  return { ...data, inputs };
}

export function retireSession(data) {
  const inputs = Object.fromEntries(Object.entries(data.inputs || {}).filter(([, item]) => item.phase !== "submitting"));
  const queue = data.queue ? { ...data.queue, items: (data.queue.items || []).filter((item) => data.inputs?.[item.id]?.phase !== "submitting") } : data.queue;
  return reconcileSession({ ...data, inputs, queue, livePrompt: null, pendingPrompts: data.inputLifecycle ? {} : data.pendingPrompts, failedPrompts: [], runtime: undefined });
}

export function projectSession(data) {
  const inputs = Object.values(data.inputs || {});
  const timeline = inputs.filter((item) => visible.has(item.phase) && !(item.phase === "submitting" && item.delivery === "queue"));
  const items = inputs.filter((item) => queued.has(item.phase) || (item.phase === "submitting" && item.delivery === "queue"))
    .sort((a, b) => (a.position ?? Infinity) - (b.position ?? Infinity) || a.id.localeCompare(b.id))
    .map((item) => ({ ...item, paused: item.phase === "paused" }));
  return { timeline, queue: { ...(data.queue || {}), items, held: data.held ?? null, inputRevision: data.inputRevision || 0 } };
}

export function timelineInputs(data) {
  if (!data?.inputLifecycle) return [...(data?.failedPrompts || []), ...(data?.livePrompt ? [data.livePrompt] : [])];
  const saved = new Set((data.messages || []).map((message) => message.inputID));
  return projectSession(data).timeline.filter((item) => !saved.has(item.id));
}

export function reconcileSession(data) {
  if (!data.inputLifecycle) return data;
  const { queue } = projectSession(data);
  return { ...data, queue, livePrompt: null, pendingPrompts: {}, failedPrompts: [] };
}

export function optimisticRuntime(state, id, patch, token) {
  const prior = state.active[id];
  return { ...state, active: { ...state.active, [id]: { ...prior, ...patch, optimistic: token } } };
}

export function rollbackRuntime(state, id, token, prior) {
  if (state.active[id]?.optimistic !== token) return state;
  const active = { ...state.active };
  if (prior) active[id] = prior;
  else delete active[id];
  return { ...state, active };
}

export function queueSummary(queue) {
  const items = queue?.items || [];
  return {
    total: items.length,
    steering: items.filter((item) => item.delivery === "steer" && !item.paused).length,
    queued: items.filter((item) => item.delivery === "queue" && !item.paused).length,
    paused: items.filter((item) => item.paused).length,
    revision: queue?.revision || 0,
    ...(queue?.held ? { held: queue.held } : {}),
  };
}

export function projectSessionSummary(session, data) {
  return session && data?.inputLifecycle ? { ...session, queue: queueSummary(projectSession(data).queue) } : session;
}
