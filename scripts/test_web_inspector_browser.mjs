import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WEB = path.join(ROOT, "src/yoke/web");
const OUTPUT = path.join(ROOT, ".agents_local/inspector-ux/browser");
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const contentTypes = { ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const httpRequests = [];
const fixtureHTML = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Inspector browser fixture</title><link rel="stylesheet" href="/assets/css/base.css"><link rel="stylesheet" href="/assets/css/layout.css"><link rel="stylesheet" href="/assets/css/session.css"><link rel="stylesheet" href="/assets/css/inspector.css"><script type="importmap">{"imports":{"preact":"/assets/vendor/preact.module.js","preact/hooks":"/assets/vendor/hooks.module.js"}}</script></head><body><div id="app"></div><script type="module" src="/__fixture.js"></script></body></html>`;

const server = createServer(async (request, response) => {
  httpRequests.push(request.url);
  response.on("finish", () => httpRequests.push(`finished ${response.statusCode} ${request.url}`));
  try {
    const url = new URL(request.url, "http://fixture");
    if (url.pathname === "/") { response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Content-Length": Buffer.byteLength(fixtureHTML), "Connection": "close" }); response.end(fixtureHTML); return; }
    const file = url.pathname === "/__fixture.js" ? path.join(ROOT, "scripts/fixtures/inspector-browser.js") : path.resolve(WEB, `.${url.pathname}`);
    if (url.pathname !== "/__fixture.js" && !file.startsWith(`${WEB}${path.sep}`)) throw new Error("Outside fixture root");
    response.setHeader("Content-Type", contentTypes[path.extname(file)] || "application/octet-stream");
    response.end(await readFile(file));
  } catch { response.statusCode = 404; response.end("Not found"); }
});

class DevTools {
  constructor(socket) {
    this.socket = socket; this.nextID = 0; this.pending = new Map(); this.errors = []; this.network = [];
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id); clearTimeout(pending.timer);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error))); else pending.resolve(message.result);
      } else if (message.method === "Runtime.exceptionThrown") this.errors.push(message.params.exceptionDetails);
      else if (["Network.responseReceived", "Network.loadingFailed", "Page.frameNavigated"].includes(message.method)) this.network.push({ method: message.method, params: message.params });
    });
  }
  send(method, params = {}, sessionId = this.sessionID) {
    const id = ++this.nextID;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`DevTools command timed out: ${method}`)); }, method === "Page.navigate" ? 60000 : 15000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result?.value;
  }
  async wait(expression) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) { if (await this.evaluate(expression)) return; await delay(40); }
    throw new Error(`Browser condition timed out: ${expression}`);
  }
  async key(key, extra = {}) {
    const codes = { Enter: 13, Tab: 9, Escape: 27, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39 };
    await this.send("Input.dispatchKeyEvent", { type: "keyDown", key, windowsVirtualKeyCode: codes[key], ...extra });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", key, windowsVirtualKeyCode: codes[key] });
  }
  async screenshot(name) {
    const image = await this.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(path.join(OUTPUT, `${name}.png`), Buffer.from(image.data, "base64"));
  }
}

let chrome;
let client;
let profile;
const passed = [];
try {
  await mkdir(OUTPUT, { recursive: true });
  profile = await mkdtemp(path.join(tmpdir(), "yoke-inspector-browser-"));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = `http://127.0.0.1:${server.address().port}/`;
  let chromeLog = "";
  chrome = spawn(process.env.CHROME_BIN || "google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--no-proxy-server", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
  const endpoint = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Chrome did not start: ${chromeLog}`)), 15000);
    chrome.on("error", (error) => { clearTimeout(timer); reject(error); });
    chrome.stderr.on("data", (chunk) => {
      chromeLog += chunk;
      const match = chromeLog.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
  });
  const socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  client = new DevTools(socket);
  const target = await client.send("Target.createTarget", { url: "about:blank" });
  client.sessionID = (await client.send("Target.attachToTarget", { targetId: target.targetId, flatten: true })).sessionId;
  await client.send("Runtime.enable"); await client.send("Page.enable"); await client.send("Network.enable");
  await client.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await client.send("Emulation.setTimezoneOverride", { timezoneId: "Europe/Brussels" });
  await client.send("Page.navigate", { url: address });
  await client.wait("Boolean(window.audit)");

  const test = async (name, fn) => { await fn(); passed.push(name); console.log(`PASS ${name}`); };
  const open = async (mode, payload = {}) => {
    await client.evaluate(`audit.controller.openInspector(${JSON.stringify(mode)}, ${JSON.stringify(payload)})`);
    await client.wait(`document.querySelector('.inspector__body--${mode}') && !document.querySelector('.inspector-loading')`);
    await delay(80);
  };

  await test("newest page is visible and latest call is selected", async () => {
    await client.evaluate("document.getElementById('fixture-opener').focus(); document.getElementById('fixture-opener').click()");
    await client.wait("audit.store.getState().sessionData[audit.sessionID]?.toolDetail?.id === 'fixture-call-205'");
    assert.equal(await client.evaluate("audit.store.getState().sessionData[audit.sessionID].toolCalls[0].sequence"), 106);
    assert.ok(await client.evaluate("document.querySelector('.activity-direction').textContent.includes('Oldest to newest')"));
    assert.ok(await client.evaluate("document.querySelector('[data-call-id=\"fixture-call-205\"]').classList.contains('is-selected')"));
    await client.screenshot("tool-activity-desktop");
  });
  await test("tool detail stays technical and fully open", async () => {
    await client.evaluate("audit.controller.selectToolCall(audit.sessionID, 'fixture-call-201')");
    await client.wait("audit.store.getState().sessionData[audit.sessionID]?.toolDetail?.id === 'fixture-call-201'");
    assert.ok(await client.evaluate("document.querySelector('.tool-signature')?.textContent.includes('exec_command')"));
    assert.equal(await client.evaluate("document.querySelector('.tool-signature')?.textContent.includes('timeout')"), false);
    assert.equal(await client.evaluate("Boolean(document.querySelector('.tool-normalization'))"), false);
    assert.ok(await client.evaluate("document.querySelector('.activity-projected-json')?.textContent.includes('24 passed in 2.18s')"));
    assert.equal(await client.evaluate("Boolean(document.querySelector('.activity-projected-json .tool-value__key'))"), true);
    assert.equal(await client.evaluate("document.querySelector('.activity-result-section')?.textContent.includes('original_token_count')"), false);
    assert.equal(await client.evaluate("document.querySelector('.activity-result-section')?.textContent.includes('wall_time_seconds')"), false);
    assert.equal(await client.evaluate("document.querySelectorAll('.activity-detail details').length"), 0);
    assert.equal(await client.evaluate("document.querySelectorAll('.activity-detail select').length"), 0);
    assert.equal(await client.evaluate("document.querySelector('.activity-detail')?.textContent.includes('Intent')"), false);
    await client.screenshot("tool-call-technical-desktop");
  });
  await test("TOON results render without canonical search bookkeeping", async () => {
    await client.evaluate("audit.controller.selectToolCall(audit.sessionID, 'fixture-call-203')");
    await client.wait("audit.store.getState().sessionData[audit.sessionID]?.toolDetail?.id === 'fixture-call-203'");
    assert.ok(await client.evaluate("document.querySelector('.activity-model-result')?.textContent.includes('matches[1]{path,line,text}')"));
    assert.equal(await client.evaluate("document.querySelector('.activity-result-section')?.textContent.includes('--json')"), false);
    assert.equal(await client.evaluate("document.querySelector('.activity-result-section')?.textContent.includes('exit_code')"), false);
    await client.screenshot("tool-call-toon-desktop");
  });
  await test("selected history and scroll survive live calls and tab switches", async () => {
    await client.evaluate("audit.controller.selectToolCall(audit.sessionID, 'fixture-call-130')");
    await client.wait("audit.store.getState().sessionData[audit.sessionID].toolDetail?.id === 'fixture-call-130'");
    await client.evaluate("const list = document.querySelector('.activity-list'); list.scrollTop = 250; list.dispatchEvent(new Event('scroll')); audit.appendCall(); audit.controller.listToolCalls(audit.sessionID)");
    assert.equal(await client.evaluate("audit.store.getState().ui.inspector.callID"), "fixture-call-130");
    await open("skills"); await open("tool");
    assert.equal(await client.evaluate("audit.store.getState().ui.inspector.callID"), "fixture-call-130");
    assert.ok(await client.evaluate("document.querySelector('[data-call-id=\"fixture-call-130\"]').classList.contains('is-selected')"));
    assert.ok(await client.evaluate("document.querySelector('.activity-list').scrollTop > 0"));
  });
  await test("a history failure does not hide an independently loaded call", async () => {
    await client.evaluate("audit.originalToolCalls = audit.api.toolCalls; audit.api.toolCalls = async () => { throw new Error('History temporarily unavailable'); }; audit.controller.setSessionField(audit.sessionID, 'toolCalls', null); audit.controller.openInspector('tool', {callID: 'fixture-call-202'})");
    await client.wait("document.querySelector('.tool-signature')?.textContent.includes('tool.js')");
    assert.ok(await client.evaluate("document.querySelector('.inspector-load-error')?.textContent.includes('History temporarily unavailable')"));
    await client.evaluate("audit.api.toolCalls = audit.originalToolCalls");
    await open("tool");
  });
  await test("Tree selects before navigating and rejects repeated Enter", async () => {
    await open("tree");
    await client.wait("Boolean(document.querySelector('.tree-entry.is-current'))");
    assert.ok(await client.evaluate("document.querySelector('.tree-entry.is-current').textContent.includes('Technical anchor')"));
    await client.evaluate("document.querySelector('.tree-entry.is-current .tree-entry__summary').focus()");
    await client.key("ArrowUp");
    await client.wait("document.querySelector('.tree-continue') && !document.querySelector('.tree-continue').disabled");
    assert.equal(await client.evaluate("audit.mutations.filter(item => item.kind === 'navigate').length"), 0);
    await client.key("Enter", { autoRepeat: true });
    assert.equal(await client.evaluate("audit.mutations.filter(item => item.kind === 'navigate').length"), 0);
    await client.screenshot("tree-destination-desktop");
    await client.key("Enter");
    await client.wait("audit.mutations.filter(item => item.kind === 'navigate').length === 1");
    assert.equal(await client.evaluate("audit.mutations.find(item => item.kind === 'navigate').targetID"), "node-24");
  });
  await test("rapid Tree selection cannot commit an old destination", async () => {
    await client.evaluate("audit.previewDelays['node-22'] = 300; audit.previewDelays['node-20'] = 80; [...document.querySelectorAll('.tree-entry__summary')].find(node => node.textContent.includes('Step 22 is ready')).click(); [...document.querySelectorAll('.tree-entry__summary')].find(node => node.textContent.includes('Step 20 is ready')).click()");
    await delay(350);
    await client.wait("document.querySelector('.tree-continue') && !document.querySelector('.tree-continue').disabled");
    await client.evaluate("document.querySelector('.tree-continue').click()");
    await client.wait("audit.mutations.filter(item => item.kind === 'navigate').length === 2");
    assert.equal(await client.evaluate("audit.mutations.filter(item => item.kind === 'navigate').at(-1).targetID"), "node-20");
  });
  await test("all supporting views render real components without exceptions", async () => {
    for (const mode of ["process", "tools", "skills", "mcp", "context"]) {
      await open(mode);
      await client.screenshot(`${mode}-desktop`);
      assert.equal(await client.evaluate("Boolean(document.querySelector('.inspector-load-error'))"), false);
    }
  });
  await test("rapid renewed process selection keeps the user's final choice", async () => {
    await open("process");
    await client.evaluate("[...document.querySelectorAll('.process-filter button')].find(node => node.textContent.includes('All')).click()");
    await delay(60);
    await client.evaluate("audit.originalProcess = audit.api.process; audit.api.process = async id => { await new Promise(resolve => setTimeout(resolve, id === 'process-1' ? 160 : 30)); return audit.originalProcess(id); }; const rows = [...document.querySelectorAll('.process-list .process-row')]; const a = rows.find(node => node.textContent.includes('pytest')); const b = rows.find(node => node.textContent.includes('git diff')); a.click(); b.click(); a.click()");
    await client.wait("audit.store.getState().sessionData[audit.sessionID].processDetail?.processID === 'process-1'");
    await delay(180);
    assert.ok(await client.evaluate("document.querySelector('.process-detail-header h2')?.textContent.includes('pytest')"));
    await client.evaluate("audit.api.process = audit.originalProcess");
  });
  await test("missing runtime process links never show an unrelated process's controls", async () => {
    await open("process", { runtimeSessionID: 999, from: { mode: "tool", callID: "fixture-call-201" } });
    assert.equal(await client.evaluate("audit.store.getState().sessionData[audit.sessionID].processDetail"), null);
    assert.equal(await client.evaluate("Boolean(document.querySelector('.process-controls'))"), false);
    assert.ok(await client.evaluate("document.querySelector('.inspector-load-error')?.textContent.includes('no longer retained')"));
  });
  await test("watched process retains its final output after exiting", async () => {
    await open("process", { processID: "process-1" });
    await client.wait("Boolean(document.querySelector('.process-output'))");
    await client.evaluate("audit.finishProcess(); Promise.all([audit.controller.refreshProcesses(audit.sessionID), audit.controller.refreshProcess('process-1')])");
    await client.wait("document.querySelector('.process-output')?.textContent.includes('All tests passed')");
  });
  await test("current-file drilldown returns to the originating call", async () => {
    await open("tool", { callID: "fixture-call-202" });
    await open("file", { path: "/fixture/yoke/src/inspector.js", from: { mode: "tool", callID: "fixture-call-202" } });
    assert.match(await client.evaluate("document.querySelector('.inspector__body--file').textContent"), /current|disk/i);
    await client.screenshot("file-desktop");
    await client.evaluate("audit.controller.backInspector()");
    await client.wait("audit.store.getState().sessionData[audit.sessionID].toolDetail?.id === 'fixture-call-202'");
  });
  await test("modal traps focus and nested palette restores it", async () => {
    await client.evaluate("document.querySelector('.inspector__close').focus()");
    for (let index = 0; index < 32; index += 1) {
      await client.key("Tab");
      assert.ok(await client.evaluate("document.querySelector('.inspector').contains(document.activeElement)"));
    }
    await client.evaluate("audit.controller.togglePalette(true)");
    await client.wait("document.querySelector('.command-palette')?.contains(document.activeElement)");
    await client.key("Escape");
    await client.wait("!document.querySelector('.command-palette')");
    assert.ok(await client.evaluate("document.querySelector('.inspector').contains(document.activeElement)"));
    assert.ok(await client.evaluate("Boolean(document.querySelector('#background-input').closest('[inert]'))"));
  });
  await test("narrow-screen views fit and preserve timestamps", async () => {
    await client.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await open("tool");
    await client.screenshot("tool-activity-mobile");
    await open("tree");
    assert.ok(await client.evaluate("[...document.querySelectorAll('.tree-entry__time')].some(node => node.getClientRects().length && getComputedStyle(node).display !== 'none')"));
    await client.screenshot("tree-mobile");
    for (const mode of ["process", "tools", "mcp", "context"]) {
      await open(mode);
      assert.ok(await client.evaluate("document.querySelector('.inspector').scrollWidth <= document.querySelector('.inspector').clientWidth + 1"));
      await client.screenshot(`${mode}-mobile`);
    }
  });
  await test("closing restores focus and unlocks the background", async () => {
    await client.evaluate("audit.controller.closeInspector()");
    await client.wait("!document.querySelector('.inspector')");
    assert.equal(await client.evaluate("document.activeElement.id"), "fixture-opener");
    assert.equal(await client.evaluate("Boolean(document.querySelector('#background-input').closest('[inert]'))"), false);
  });
  await test("chat displays saved failures and cancellations on desktop and mobile", async () => {
    await client.evaluate(`(async () => {
      const { html, render } = await import('/assets/vendor/htm-preact.js');
      const { Timeline } = await import('/assets/js/session/timeline.js');
      const root = document.createElement('div');
      root.id = 'chat-qa';
      document.getElementById('app').hidden = true;
      document.body.append(root);
      const outcomes = [
        ['failed', 'exec_command', { ok: true, exit_code: 2 }],
        ['cancelled', 'read', { ok: false, cancelled: true }],
        ['completed', 'rg', { ok: true, exit_code: 1 }],
      ];
      const messages = outcomes.flatMap(([id, name, result]) => [
        { id: 'a-' + id, type: 'assistant', toolCalls: [{ id, name, arguments: '{}' }] },
        { id: 't-' + id, type: 'tool', callID: id, result: JSON.stringify(result) },
      ]);
      render(html\`<\${Timeline} sessionID="chat-qa" data=\${{ loaded: true, messages }} />\`, root);
    })()`);
    await client.wait("document.querySelectorAll('#chat-qa [data-tool-call-id]').length === 3");
    for (const width of [1440, 390]) {
      await client.send("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 500 });
      const rows = await client.evaluate(`Array.from(document.querySelectorAll('#chat-qa [data-tool-call-id]'), row => ({
        id: row.dataset.toolCallId, state: row.querySelector('.tool-line__state').textContent,
        glyph: row.querySelector('.tool-line__glyph').textContent,
        color: getComputedStyle(row.querySelector('.tool-line__state')).color,
      }))`);
      assert.deepEqual(rows.map(({ id, state, glyph }) => [id, state, glyph]), [
        ['failed', 'failed', '×'], ['cancelled', 'cancelled', '·'], ['completed', 'done', '✓'],
      ]);
      assert.notEqual(rows[0].color, rows[2].color);
      await client.screenshot(`chat-tool-outcomes-${width}`);
    }
  });
  assert.deepEqual(await client.evaluate("audit.unexpected"), []);
  assert.deepEqual(client.errors, []);
  await writeFile(path.join(OUTPUT, "results.json"), JSON.stringify({ tests: passed, screenshots: OUTPUT }, null, 2));
  console.log(JSON.stringify({ tests: passed.length, screenshots: OUTPUT }));
} catch (error) {
  if (client) {
    await client.screenshot("failure").catch(() => {});
    await writeFile(path.join(OUTPUT, "failure.json"), JSON.stringify({ error: String(error), httpRequests, exceptions: client.errors, network: client.network, frame: await client.send("Page.getFrameTree").catch(() => null), body: await client.evaluate("document.body.innerText").catch(() => "") }, null, 2));
  }
  throw error;
} finally {
  if (client) { await client.evaluate("window.audit?.stop()").catch(() => {}); client.socket.close(); }
  if (chrome) { chrome.kill("SIGTERM"); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(1000)]); }
  await new Promise((resolve) => server.close(resolve));
  if (profile) await rm(profile, { recursive: true, force: true, maxRetries: 4, retryDelay: 150 });
}
