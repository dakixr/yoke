import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function ProcessInspector({ sessionID, data, capabilities }) {
  const processes = data?.processes;
  const detail = data?.processDetail;
  const [stdin, setStdin] = useState("");
  if (!processes) return html`<div class="inspector-loading">Loading managed processes…</div>`;
  return html`<div class="inspector-stack">
    <div class="inspector-meta"><span>${processes.length} retained processes</span><button onClick=${() => controller.refreshProcesses(sessionID)}>Refresh</button></div>
    <div class="activity-list">
      ${processes.map((process) => html`<button class=${`activity-row ${detail?.processID === process.processID ? "is-selected" : ""}`} onClick=${() => controller.loadProcess(process.processID)}>
        <span class="activity-row__main"><strong>${process.command}</strong><span>${process.cwd}</span></span><span class=${`status-pill status-pill--${process.status}`}>${process.status}</span>
      </button>`)}
    </div>
    ${detail ? html`<section class="process-detail">
      <div class="inspector-section-title">Process ${detail.pid}</div>
      <dl class="detail-grid"><dt>Command</dt><dd><code>${detail.command}</code></dd><dt>CWD</dt><dd>${detail.cwd}</dd><dt>Status</dt><dd>${detail.status}</dd><dt>TTY</dt><dd>${detail.tty ? "yes" : "no"}</dd><dt>Elapsed</dt><dd>${detail.elapsedMs} ms</dd><dt>Exit</dt><dd>${detail.exitCode ?? "—"}</dd></dl>
      ${!capabilities?.features?.pty ? html`<p class="inspector-note">Managed process controls only. PTY terminal emulation is not enabled by this server.</p>` : null}
      <pre class="output-pre">${(detail.outputChunks || []).map((chunk) => chunk.text).join("") || detail.output?.tail || "No retained output."}</pre>
      ${detail.status === "running" ? html`<div class="process-controls">
        <label class="stacked-label">stdin<textarea rows="2" value=${stdin} onInput=${(event) => setStdin(event.currentTarget.value)}></textarea></label>
        <div class="button-row"><button disabled=${!stdin} onClick=${() => controller.sendProcessInput(detail.processID, stdin).then(() => setStdin(""))}>Write stdin</button><button onClick=${() => controller.signalProcess(detail.processID, "interrupt")}>Interrupt</button><button class="danger-text" onClick=${() => controller.signalProcess(detail.processID, "terminate")}>Terminate</button></div>
      </div>` : null}
    </section>` : null}
  </div>`;
}
