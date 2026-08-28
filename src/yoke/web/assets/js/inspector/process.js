import { html, useEffect, useLayoutEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function ProcessInspector({ sessionID, data, capabilities }) {
  const processes = data?.processes;
  const detail = data?.processDetail;
  const [stdin, setStdin] = useState("");
  const [wrapOutput, setWrapOutput] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const outputRef = useRef(null);
  const initialSelectionRef = useRef(false);
  const followingOutputRef = useRef(true);

  useEffect(() => {
    if (!processes?.length || initialSelectionRef.current) return;
    initialSelectionRef.current = true;
    const newest = processes.at(-1);
    if (detail?.processID === newest.processID) return;
    void controller.loadProcess(newest.processID).catch((error) => controller.notice(error?.message || String(error)));
  }, [processes]);

  useEffect(() => {
    followingOutputRef.current = true;
    setStdin("");
  }, [detail?.processID]);

  const outputText = detail
    ? (detail.outputChunks || []).map((chunk) => chunk.text).join("") || detail.output?.tail || ""
    : "";

  useLayoutEffect(() => {
    const node = outputRef.current;
    if (!node || !followingOutputRef.current) return;
    node.scrollTop = node.scrollHeight;
  }, [detail?.processID, outputText.length]);

  if (!processes) return html`<div class="inspector-loading">Loading managed processes…</div>`;

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await controller.refreshProcesses(sessionID);
      if (detail?.processID) await controller.loadProcess(detail.processID);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setRefreshing(false);
    }
  };

  return html`<div class="process-inspector">
    <aside class="process-sidebar">
      <div class="process-sidebar__header">
        <div><strong>Command processes</strong><span>${processes.length} retained</span></div>
        <button class="quiet-button" disabled=${refreshing} onClick=${refresh}>
          ${refreshing ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
          <span>${refreshing ? "Refreshing" : "Refresh"}</span>
        </button>
      </div>
      <div class="process-list" role="list" aria-label="Managed command processes">
        ${[...processes].reverse().map((process) => html`<button
          key=${process.processID}
          class=${`process-row ${detail?.processID === process.processID ? "is-selected" : ""}`}
          onClick=${() => controller.loadProcess(process.processID)}
        >
          <span class=${`process-row__status process-row__status--${process.status}`} aria-hidden="true">${statusGlyph(process.status)}</span>
          <span class="process-row__main">
            <strong>${singleLine(process.command)}</strong>
            <span>${shortPath(process.cwd)}</span>
          </span>
          <span class="process-row__meta">${process.status}</span>
        </button>`)}
        ${!processes.length ? html`<div class="process-empty">No command processes have been started in this session.</div>` : null}
      </div>
    </aside>

    <section class="process-main">
      ${detail ? html`
        <header class="process-hero">
          <div class="process-hero__title">
            <span class=${`process-status-dot process-status-dot--${detail.status}`} aria-hidden="true"></span>
            <div><span class="process-hero__eyebrow">Process ${detail.processID}</span><h2>${singleLine(detail.command)}</h2></div>
          </div>
          <span class=${`status-pill status-pill--${detail.status}`}>${detail.status}</span>
        </header>

        <div class="process-facts">
          <${ProcessFact} label="PID" value=${detail.pid ?? "—"} />
          <${ProcessFact} label="Elapsed" value=${formatDuration(detail.elapsedMs)} />
          <${ProcessFact} label="Exit" value=${detail.exitCode ?? "—"} />
          <${ProcessFact} label="TTY" value=${detail.tty ? "yes" : "no"} />
        </div>

        <div class="process-path"><span>Working directory</span><code>${detail.cwd}</code></div>

        <section class="process-command-card">
          <div class="inspector-section-title">Command</div>
          <pre>${detail.command}</pre>
        </section>

        <section class="process-output-card">
          <div class="process-output-card__header">
            <div><div class="inspector-section-title">Output</div><span>${outputText ? `${outputText.length.toLocaleString()} retained characters` : "No retained output yet"}</span></div>
            <button class=${wrapOutput ? "is-active" : ""} aria-pressed=${wrapOutput} onClick=${() => setWrapOutput((value) => !value)}>${wrapOutput ? "Wrap on" : "Wrap off"}</button>
          </div>
          <pre
            ref=${outputRef}
            class=${`process-output ${wrapOutput ? "is-wrapped" : ""}`}
            onScroll=${(event) => {
              const node = event.currentTarget;
              followingOutputRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
            }}
          >${outputText || "No retained output."}</pre>
        </section>

        ${!capabilities?.features?.pty ? html`<p class="inspector-note">Managed process controls are available. PTY terminal emulation is not enabled by this server.</p>` : null}

        ${detail.status === "running" ? html`<div class="process-controls">
          <label class="stacked-label process-stdin">stdin<textarea rows="2" value=${stdin} placeholder="Write to the running process…" onInput=${(event) => setStdin(event.currentTarget.value)}></textarea></label>
          <div class="button-row process-controls__actions">
            <button class="primary" disabled=${!stdin} onClick=${() => controller.sendProcessInput(detail.processID, stdin).then(() => setStdin(""))}>Write stdin</button>
            <button onClick=${() => controller.signalProcess(detail.processID, "interrupt")}>Interrupt</button>
            <button class="danger-text" onClick=${() => controller.signalProcess(detail.processID, "terminate")}>Terminate</button>
          </div>
        </div>` : null}
      ` : html`<div class="process-empty process-empty--detail">Select a process to inspect its command, output, and controls.</div>`}
    </section>
  </div>`;
}

function ProcessFact({ label, value }) {
  return html`<div class="process-fact"><span>${label}</span><strong>${value}</strong></div>`;
}

function statusGlyph(status) {
  if (status === "running" || status === "pending") return "…";
  if (status === "failed" || status === "error" || status === "cancelled") return "×";
  return "✓";
}

function singleLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim() || "Command";
}

function shortPath(value) {
  const path = String(value || "");
  if (path.length <= 58) return path;
  return `…${path.slice(-55)}`;
}

function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
}
