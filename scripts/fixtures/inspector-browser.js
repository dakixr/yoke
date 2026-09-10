import { html, render } from "/assets/vendor/htm-preact.js";
import { Inspector } from "/assets/js/inspector/inspector.js";
import { CommandPalette } from "/assets/js/components/command-palette.js";
import { controller } from "/assets/js/state/controller.js";
import { store } from "/assets/js/state/store.js";
import { api, ApiError } from "/assets/js/api/client.js";
import { installKeybindings } from "/assets/js/lib/keyboard.js";

// All API calls are in-memory fixtures. This page cannot mutate a real session.
const SESSION = "inspector-fixture";
const ROOT = "/fixture/yoke";
const date = (seconds) => new Date(Date.parse("2026-09-10T09:10:00Z") + seconds * 1000).toISOString();
const copy = (value) => structuredClone(value);
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mutations = [];
const unexpected = [];
const previewDelays = {};
const calls = Array.from({ length: 205 }, (_, index) => makeCall(index + 1));
let revision = 1234;
let leafID = "node-25";
const treeEntries = Array.from({ length: 36 }, (_, index) => {
  const number = index + 1;
  return {
    id: `node-${number}`, parentID: number === 1 ? null : number === 26 ? "node-8" : `node-${number - 1}`,
    kind: number === 25 || number % 7 === 0 ? "tool" : number % 2 ? "user" : "assistant",
    phase: number % 2 === 0 ? "final" : null, createdAt: date(number * 20),
    preview: number % 2 ? `Review the inspector changes, step ${number}. Keep recent calls visible and preserve the selected destination.`
      : `Step ${number} is ready. The history is chronological, and selecting a destination does not move the conversation until confirmed.`,
    label: number === 12 ? "Before inspector changes" : number === 30 ? "Alternate approach" : null,
  };
});
const processes = [
  { processID: "process-1", sessionID: SESSION, runtimeSessionID: 7, command: "uv run pytest tests/yoke/http", cwd: ROOT, status: "running", startedAt: date(200), elapsedMs: 8400, pid: 4321, tty: false, output: { tail: "Collecting tests…\n", latestSeq: 1, retainedChars: 18, truncated: false } },
  { processID: "process-2", sessionID: SESSION, runtimeSessionID: 6, command: "git diff --check", cwd: ROOT, status: "exited", startedAt: date(100), elapsedMs: 48, exitCode: 0, pid: 4320, tty: false, output: { tail: "", latestSeq: 0, retainedChars: 0, truncated: false } },
];
const tools = [
  { name: "read_file", description: "Read a file or a bounded line range from the workspace.", source: "built-in", capabilityID: "file.read", enabled: true },
  { name: "exec_command", description: "Run commands and inspect their output.", source: "built-in", capabilityID: "shell", enabled: true },
  { name: "apply_patch", description: "Apply an explicit patch to workspace files.", source: "built-in", capabilityID: "file.write", enabled: true },
  { name: "image_generate", description: "Generate a new raster image.", source: "provider", capabilityID: "image.generate", enabled: false },
];
const skills = {
  active: [{ name: "unslop", description: "Write plainly and remove filler.", sourcePath: `${ROOT}/skills/unslop/SKILL.md`, active: true }],
  available: [
    { name: "unslop", description: "Write plainly and remove filler.", sourcePath: `${ROOT}/skills/unslop/SKILL.md` },
    { name: "frontend-design", description: "Design deliberate interfaces with clear typography and useful structure.", sourcePath: `${ROOT}/skills/frontend-design/SKILL.md` },
    { name: "review-agent", description: "Review changes for actionable defects.", sourcePath: `${ROOT}/skills/review-agent/SKILL.md` },
  ],
};
const servers = [
  { name: "workspace", transport: "stdio", scope: "global", status: "ready", enabled: true, enabledTools: null, disabledTools: [], tools: [{ name: "list", description: "List workspace resources." }, { name: "inspect", description: "Inspect one resource." }], truncated: false },
  { name: "build-server", transport: "http", scope: "repo", status: "error", enabled: true, enabledTools: null, disabledTools: [], tools: [], error: "The server did not respond. Check its address and connection.", truncated: false },
];
const session = {
  id: SESSION, title: "Refine the inspector experience", location: { directory: ROOT },
  selection: { provider: "fixture", model: "development", reasoningEffort: "high" },
  time: { created: date(0), updated: date(500) }, tree: { entryCount: treeEntries.length }, queue: { pending: [] },
};
const context = {
  truncated: true, retainedEntries: 4, totalEntries: 85, retainedChars: 1080, maxChars: 500000,
  messages: [
    { role: "user", content: [{ type: "text", text: "Make the tool activity inspector easier to scan. Tree should remain navigation-first." }] },
    { role: "assistant", phase: "commentary", content: [{ type: "text", text: "I will preserve canonical history order and separate the selected call from live updates." }] },
    { role: "tool", content: [{ type: "text", text: '{"ok":true,"changed":["inspector.css","tool.js"]}' }] },
    { role: "user", content: [{ type: "image", url: "fixture-only" }, { type: "text", text: "Keep timestamps visible on narrow screens too." }] },
  ],
};

function makeCall(sequence) {
  const names = ["exec_command", "read_file", "rg", "apply_patch", "mcp_call"];
  const name = names[(sequence - 1) % names.length];
  const args = name === "exec_command" ? { cmd: `uv run pytest tests/yoke/http -k case_${sequence}`, workdir: ROOT, yield_time_ms: 1000 }
    : name === "read_file" ? { path: `${ROOT}/src/yoke/web/assets/js/inspector/tool.js`, offset: sequence, limit: 40 }
      : name === "rg" ? { raw_args: `--line-number 'inspector' src/yoke`, root_dir: ROOT }
        : name === "apply_patch" ? { input: "*** Begin Patch\n*** Update File: src/inspector.js\n@@\n-old\n+new\n*** End Patch" }
          : { server: "workspace", tool: "inspect", arguments: { resource: `trace-${sequence}` } };
  const failed = sequence === 205;
  const text = failed ? "Connection timed out while connecting to workspace.\nCheck the server connection and retry."
    : name === "read_file" ? "export function inspect(session) {\n  return session.calls;\n}\n"
      : name === "rg" ? `src/inspector.js:42: export function inspect(session)\n`
        : name === "apply_patch" ? "Updated src/inspector.js\n" : "Collected 24 tests\n24 passed in 2.18s\n";
  const result = failed ? { ok: false, error: "Connection timed out", output: text }
    : name === "read_file" ? { ok: true, path: args.path, content: text, total_lines: 120, complete: true }
      : name === "apply_patch" ? { ok: true, changes: [{ action: "M", path: "src/inspector.js" }], changes_applied: 1, stdout: text }
        : name === "exec_command" ? {
          ok: true, exit_code: 0, returncode: 0, running: false, wall_time_seconds: 2.18,
          elapsed_seconds: 2.18, original_token_count: 812, output: text,
          outputTruncationDetails: { truncated: false, totalLines: 2, outputLines: 2 }, processID: "process-1",
        }
          : name === "rg" ? { ok: true, command: ["rg", "--json", "inspector"], output: [{ kind: "match", path: "src/inspector.js", line: 42, text: "export function inspect(session)" }], exit_code: 0 }
            : { ok: true, exit_code: 0, output: text };
  const resultProjection = name === "exec_command"
    ? JSON.stringify({ ok: true, output: text })
    : name === "rg"
      ? 'ok: true\nmatches[1]{path,line,text}:\nsrc/inspector.js,42,"export function inspect(session)"'
      : name === "apply_patch"
        ? "ok: true\nchanges[1\t]{action\tpath}:\nM\tsrc/inspector.js"
        : null;
  return {
    id: `fixture-call-${sequence}`, sequence, toolName: name, status: failed ? "failed" : "ok",
    turnID: Math.ceil(sequence / 6), iteration: sequence % 6 + 1,
    arguments: { raw: JSON.stringify(args), executed: { ...args, ...(name === "exec_command" ? { timeout: 10000 } : {}) } },
    result, resultProjection,
    time: { started: sequence < 120 ? null : date(sequence), ended: sequence < 120 ? null : date(sequence + 2), durationMs: sequence < 120 ? null : 2180 },
    retention: sequence < 120 ? "session" : "runtime",
    output: { retainedChars: text.length, truncated: false, latestSeq: 1 },
    outputChunks: [{ seq: 1, stream: "stdout", text }], context: [], afterContext: [],
  };
}

function tree() {
  const active = new Set();
  let current = treeEntries.find((entry) => entry.id === leafID);
  while (current) { active.add(current.id); current = treeEntries.find((entry) => entry.id === current.parentID); }
  return treeEntries.map((entry) => ({ ...entry, current: entry.id === leafID, active: active.has(entry.id), childCount: treeEntries.filter((child) => child.parentID === entry.id).length }));
}

api.request = async (path) => { unexpected.push(path); throw new Error(`Unstubbed fixture request: ${path}`); };
api.toolCalls = async (_id, { limit = 100, cursor } = {}) => {
  const end = cursor ? Number(cursor) : calls.length;
  const start = Math.max(0, end - limit);
  return { data: copy(calls.slice(start, end)), total: calls.length, cursor: { next: start ? String(start) : null } };
};
api.toolCall = async (_id, callID) => {
  const found = calls.find((call) => call.id === callID);
  if (!found) throw new ApiError(404, "tool_call_not_found", "Tool call was not found.");
  return { data: copy(found) };
};
api.toolOutput = async (_id, callID, afterSeq = 0) => ({ data: copy(calls.find((call) => call.id === callID)?.outputChunks.filter((chunk) => chunk.seq > afterSeq) || []), cursor: { next: 1, truncatedBefore: 0 } });
api.tree = async (_id, { limit = 80, cursor } = {}) => {
  const entries = tree();
  const end = cursor ? Number(cursor) : entries.length;
  const start = Math.max(0, end - limit);
  return { data: { revision, leafID, entries: copy(entries.slice(start, end)), totalEntries: entries.length, cursor: { next: start ? String(start) : null } } };
};
api.treePreview = async (_id, targetID) => {
  await pause(previewDelays[targetID] || 0);
  const entries = tree();
  const targetPath = new Set();
  let node = entries.find((entry) => entry.id === targetID);
  while (node) { targetPath.add(node.id); node = entries.find((entry) => entry.id === node.parentID); }
  const abandoned = entries.filter((entry) => entry.active && !targetPath.has(entry.id));
  return { data: { targetID, current: targetID === leafID, abandoned: copy(abandoned), abandonedTotal: abandoned.length, abandonedTruncated: false, editorText: null } };
};
api.navigateTree = async (_id, body) => {
  if (body.expectedRevision !== revision) throw new ApiError(409, "tree_revision_conflict", "Tree changed.");
  mutations.push({ kind: "navigate", ...body });
  leafID = body.targetID;
  revision += 1;
  return { data: { leafID, revision, editorText: null } };
};
api.patchTreeEntry = async (_id, entryID, body) => {
  mutations.push({ kind: "label", entryID, ...body });
  treeEntries.find((entry) => entry.id === entryID).label = body.label;
  revision += 1;
  return { data: { revision, entry: tree().find((entry) => entry.id === entryID) } };
};
api.messages = async () => ({ data: [], cursor: { next: null } });
api.getSession = async () => ({ data: copy(session) });
api.processes = async () => ({ data: copy(processes) });
api.process = async (id) => ({ data: copy(processes.find((process) => process.processID === id)) });
api.processOutput = async (id) => ({ data: [], cursor: { next: processes.find((process) => process.processID === id)?.output.latestSeq || 0, truncatedBefore: 0 } });
api.processSignal = async (id, signal) => {
  mutations.push({ kind: "signal", id, signal });
  const process = processes.find((process) => process.processID === id);
  process.status = "exited";
  process.exitCode = 130;
  return { data: copy(process) };
};
api.processStdin = async (id, text) => { mutations.push({ kind: "stdin", id, text }); return { data: copy(processes.find((process) => process.processID === id)) }; };
api.tools = async () => ({ data: copy(tools) });
api.patchTools = async (_id, patch) => {
  mutations.push({ kind: "tools", ...patch });
  for (const tool of tools) { if (patch.enabled.includes(tool.name)) tool.enabled = true; if (patch.disabled.includes(tool.name)) tool.enabled = false; }
  return { data: { enabled: tools.filter((tool) => tool.enabled).map((tool) => tool.name) } };
};
api.sessionSkills = async () => ({ data: copy(skills) });
api.activateSkill = async (_id, name) => {
  const skill = { ...skills.available.find((skill) => skill.name === name), active: true };
  skills.active.push(skill);
  mutations.push({ kind: "skill", name });
  return { data: { activated: copy(skill) } };
};
api.sessionMcp = async () => ({ data: copy(servers) });
api.patchMcp = async (_id, name, patch) => {
  mutations.push({ kind: "mcp", name, ...patch });
  Object.assign(servers.find((server) => server.name === name), patch);
  return { data: copy(servers.find((server) => server.name === name)) };
};
api.context = async () => ({ data: copy(context) });
api.fsRead = async (_directory, path) => path.endsWith("SKILL.md")
  ? "# Skill instructions\n\nUse clear language. Preserve the user's intent.\n"
  : Array.from({ length: 90 }, (_, index) => `// Line ${index + 1}\nconst value${index + 1} = ${index + 1};`).join("\n");

store.setState((state) => ({
  ...state, sessions: { [SESSION]: session }, sessionData: { [SESSION]: {} },
  connection: { ...state.connection, current: true, status: "connected" },
  capabilities: { features: { toolInspector: true, sessionTree: true, processInspector: true, skills: true, mcp: true, pty: false } },
  commands: [], ui: { ...state.ui, selectedSessionID: SESSION },
}));
installKeybindings({ palette: () => controller.togglePalette(true), escape: () => controller.escape() });
render(html`<div class="app-shell"><aside><button id="fixture-opener" data-inspector-opener onClick=${() => controller.openInspector("tool")}>Inspect session</button><input id="background-input" aria-label="Background input" /></aside><main class="workspace"><div>Inspector test fixture</div><${Inspector} /></main><${CommandPalette} /></div>`, document.getElementById("app"));
window.audit = {
  controller, store, api, sessionID: SESSION, mutations, unexpected, previewDelays,
  appendCall() { calls.push(makeCall(calls.length + 1)); },
  finishProcess() { processes[0].status = "exited"; processes[0].exitCode = 0; processes[0].output.tail += "All tests passed.\n"; processes[0].output.latestSeq += 1; },
  stop: () => controller.stop(),
};
