import assert from "node:assert/strict";
import { projectSessionSummary, admitSubmission, applyInputChanged, applyActiveChanged, applyQueueHeld, installInputSnapshot, projectSession, submittingInput, retireSession, isBusy } from "../src/yoke/web/assets/js/state/session-machine.js";

const started = performance.now();
const phases = ["queued", "paused", "promoted", "running", "settled", "removed"];
let cases = 0;
for (const previous of ["submitting", ...phases]) {
  for (const phase of phases) {
    const before = { inputs: { one: { id: "one", phase: previous, prompt: { text: "old" } } }, inputRevision: 1 };
    const data = { inputID: "one", phase, inputRevision: 2, prompt: { text: "new" }, position: 0 };
    const next = applyInputChanged(before, data);
    const projection = projectSession(next);
    assert.equal(next.inputRevision, 2);
    assert.equal(before.inputs.one.phase, previous, "reducer must not mutate its argument");
    assert.equal(Boolean(next.inputs.one), !["settled", "removed"].includes(phase));
    assert.equal(projection.queue.items.some((item) => item.id === "one"), ["queued", "paused"].includes(phase));
    assert.equal(projection.timeline.some((item) => item.id === "one"), ["promoted", "running"].includes(phase));
    assert.equal(applyInputChanged(next, data), next, "duplicate revisions are ignored");
    cases++;
  }
}
let acknowledged = admitSubmission(submittingInput({ inputLifecycle: true }, { id: "fast", prompt: { text: "fast" } }), { id: "fast", phase: "running" });
acknowledged = installInputSnapshot(acknowledged, { inputRevision: 3, inputs: [] });
assert.deepEqual(acknowledged.inputs, {}, "a completed admission must not leave a submitting ghost after a live snapshot");
let state = submittingInput({}, { id: "local", delivery: "steer", prompt: { text: "local" } });
state = applyInputChanged(state, { inputID: "local", phase: "queued", prompt: { text: "local" }, inputRevision: 1 });
assert.equal(projectSession(state).timeline.length, 0, "admission removes timeline optimism immediately");
state = applyInputChanged(state, { inputID: "local", phase: "running", inputRevision: 3 });
assert.equal(state.resyncPending, true, "revision gaps latch resync");
state = applyQueueHeld(state, { inputRevision: 4, held: { reason: "interrupt" } });
assert.equal(state.resyncPending, true);
state = installInputSnapshot(state, { inputRevision: 4, held: { reason: "interrupt" }, inputs: [{ id: "local", phase: "paused", prompt: "paused", position: 0 }] });
assert.equal(state.resyncPending, false);
assert.equal(projectSession(state).queue.held.reason, "interrupt");
assert.equal(projectSession(state).queue.items[0].prompt.text, "paused");
assert.equal(installInputSnapshot(state, { inputRevision: 3, inputs: [] }), state, "late snapshot must not regress inputs");
state = applyInputChanged(state, { inputID: "local", inputRevision: 5, phase: "removed" });
assert.equal(projectSession(state).queue.items.length, 0);
state = submittingInput(state, { id: "ghost", delivery: "steer", prompt: { text: "ghost" } });
assert.equal(projectSession(retireSession(state)).timeline.length, 0, "retirement removes ghost submissions");
for (const phase of ["idle", "turn_active", "retiring", "operation_active"]) {
  const runtime = applyActiveChanged({}, { phase, state: phase === "idle" ? "idle" : "running", revision: 3 });
  assert.equal(isBusy(runtime.runtime), phase !== "idle");
  assert.equal(applyActiveChanged(runtime, { state: "running", revision: 2 }), runtime);
}
const error = { code: "session_workspace_unavailable" };
assert.equal(applyActiveChanged({}, { state: "error", error }).runtime.error, error);
console.log(JSON.stringify({ cases, milliseconds: performance.now() - started }));
for (const state of ["idle", "error", "running", "waiting_input", "stopping"]) {
  assert.equal(isBusy({ state }), !["idle", "error"].includes(state));
}
const pendingPair = submittingInput(submittingInput({ inputLifecycle: true }, { id: "first", delivery: "steer" }), { id: "second", delivery: "steer" });
assert.deepEqual(projectSession(pendingPair).timeline.map((item) => item.id), ["first", "second"], "two rapid steers keep both identities");
assert.equal(projectSession(retireSession(pendingPair)).timeline.length, 0);

const heldView = installInputSnapshot({}, { inputRevision: 1, held: { reason: "interrupt" }, inputs: [{ id: "held", phase: "queued", delivery: "queue", prompt: "pending" }] });
const staleSummary = { id: "session", queue: { total: 0, revision: 1 } };
assert.equal(projectSessionSummary(staleSummary, heldView).queue.held.reason, "interrupt", "summary refresh cannot hide the machine's hold");
assert.equal(projectSessionSummary(staleSummary, heldView).queue.total, 1);
