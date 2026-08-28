import { html, useEffect, useLayoutEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function ProcessInspector({ sessionID, data, capabilities }) {
  const processes = data?.processes;
  const detail = data?.processDetail;
  const [filter, setFilter] = useState("running");
  const [stdin, setStdin] = useState("");
  const [wrapOutput, setWrapOutput] = useState(true);
  const [followingOutput, setFollowingOutput] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const outputRef = useRef(null);
  const followingOutputRef = useRef(true);

  const runningProcesses = (processes || []).filter((process) => process.status === "running");
  const visibleProcesses = filter === "running" ? runningProcesses : (processes || []);
  const selectedVisible = Boolean(
    detail?.processID && visibleProcesses.some((process) => process.processID === detail.processID),
  );
  const selectedDetail = selectedVisible ? detail : null;
  const completedCount = Math.max(0, (processes?.length || 0) - runningProcesses.length);

  useEffect(() => {
    if (!processes) return;
    if (selectedVisible) return;
    const newestVisible = visibleProcesses[0];
    if (!newestVisible) return;
    void controller.loadProcess(newestVisible.processID).catch((error) => controller.notice(error?.message || String(error)));
  }, [processes, filter, detail?.processID]);

  useEffect(() => {
    followingOutputRef.current = true;
    setFollowingOutput(true);
    setStdin("");
  }, [selectedDetail?.processID]);

  const outputText = selectedDetail?.output?.tail || "";

  useLayoutEffect(() => {
    const node = outputRef.current;
    if (!node || !followingOutputRef.current) return;
    node.scrollTop = node.scrollHeight;
  }, [selectedDetail?.processID, outputText.length]);

  if (!processes) return html`<div class="inspector-loading">Loading managed processes…</div>`;

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await Promise.all([
        controller.refreshProcesses(sessionID),
        selectedDetail?.processID ? controller.loadProcess(selectedDetail.processID) : Promise.resolve(),
      ]);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setRefreshing(false);
    }
  };

  const showAll = () => setFilter("all");

  return html`<div class="process-inspector">
    <aside class="process-sidebar">
      <div class="process-sidebar__header">
        <div class="process-sidebar__heading">
          <strong>Processes</strong>
          <span>${runningProcesses.length} running · ${processes.length} retained</span>
        </div>
        <button class="process-refresh" aria-label="Refresh processes" title="Refresh processes" disabled=${refreshing} onClick=${refresh}>
          ${refreshing ? html`<span class="pending-spinner" aria-hidden="true"></span>` : html`<span aria-hidden="true">↻</span>`}
        </button>
      </div>

      <div class="process-filter" role="group" aria-label="Process visibility">
        <button class=${filter === "running" ? "is-active" : ""} aria-pressed=${filter === "running"} onClick=${() => setFilter("running")}>
          <span>Running</span><strong>${runningProcesses.length}</strong>
        </button>
        <button class=${filter === "all" ? "is-active" : ""} aria-pressed=${filter === "all"} onClick=${showAll}>
          <span>All</span><strong>${processes.length}</strong>
        </button>
      </div>

      <div class="process-list" role="listbox" aria-label=${filter === "running" ? "Running command processes" : "All retained command processes"}>
        ${visibleProcesses.map((process) => html`<button
          key=${process.processID}
          role="option"
          aria-selected=${selectedDetail?.processID === process.processID}
          class=${`process-row ${selectedDetail?.processID === process.processID ? "is-selected" : ""}`}
          title=${`${singleLine(process.command)}\n${process.cwd}`}
          onClick=${() => controller.loadProcess(process.processID)}
        >
          <span class=${`process-row__status process-row__status--${process.status}`} aria-hidden="true">${statusGlyph(process.status)}</span>
          <span class="process-row__main">
            <span class="process-row__identity">
              <strong>#${process.runtimeSessionID}</strong>
              <span>${formatDuration(process.elapsedMs)}</span>
              <span>${statusText(process)}</span>
            </span>
            <span class="process-row__command">${singleLine(process.command)}</span>
          </span>
        </button>`)}

        ${!visibleProcesses.length && filter === "running" ? html`
          <div class="process-empty process-empty--sidebar">
            <strong>No running processes</strong>
            <span>${completedCount ? `${completedCount} completed process${completedCount === 1 ? " is" : "es are"} retained.` : "Start a background command and it will appear here."}</span>
            ${completedCount ? html`<button onClick=${showAll}>Show completed</button>` : null}
          </div>
        ` : null}
        ${!visibleProcesses.length && filter === "all" ? html`
          <div class="process-empty process-empty--sidebar"><strong>No command processes yet</strong><span>Managed background commands will appear here.</span></div>
        ` : null}
      </div>
    </aside>

    <section class="process-main">
      ${selectedDetail ? html`
        <header class="process-detail-header">
          <div class="process-detail-header__title">
            <span class=${`process-status-dot process-status-dot--${selectedDetail.status}`} aria-hidden="true"></span>
            <div>
              <div class="process-detail-header__eyebrow">Process #${selectedDetail.runtimeSessionID} · ${statusText(selectedDetail)}</div>
              <h2>${singleLine(selectedDetail.command)}</h2>
            </div>
          </div>
          <div class="process-detail-header__facts">
            <span>PID <strong>${selectedDetail.pid ?? "—"}</strong></span>
            <span>Elapsed <strong>${formatDuration(selectedDetail.elapsedMs)}</strong></span>
            <span>TTY <strong>${selectedDetail.tty ? "yes" : "no"}</strong></span>
          </div>
        </header>

        <div class="process-location-line"><span>cwd</span><code title=${selectedDetail.cwd}>${selectedDetail.cwd}</code></div>

        <section class="process-terminal">
          <div class="process-terminal__header">
            <div class="process-terminal__title">
              <strong>Output</strong>
              ${selectedDetail.status === "running" ? html`<span class="process-live"><i aria-hidden="true"></i>LIVE</span>` : html`<span>${statusText(selectedDetail)}</span>`}
              <span>${outputSummary(selectedDetail.output)}</span>
            </div>
            <button class=${wrapOutput ? "is-active" : ""} aria-pressed=${wrapOutput} onClick=${() => setWrapOutput((value) => !value)}>${wrapOutput ? "Wrap on" : "Wrap off"}</button>
          </div>
          <div class="process-terminal__viewport">
            <pre
              ref=${outputRef}
              class=${`process-output ${wrapOutput ? "is-wrapped" : ""}`}
              onScroll=${(event) => {
                const node = event.currentTarget;
                const following = node.scrollHeight - node.scrollTop - node.clientHeight < 28;
                followingOutputRef.current = following;
                setFollowingOutput(following);
              }}
            >${outputText || "Waiting for process output…"}</pre>
            ${!followingOutput && selectedDetail.status === "running" ? html`
              <button class="process-jump-live" onClick=${() => {
                followingOutputRef.current = true;
                setFollowingOutput(true);
                const node = outputRef.current;
                if (node) node.scrollTop = node.scrollHeight;
              }}>Jump to live ↓</button>
            ` : null}
          </div>
        </section>

        ${!capabilities?.features?.pty ? html`<p class="inspector-note">Managed process controls are available. PTY terminal emulation is not enabled by this server.</p>` : null}

        ${selectedDetail.status === "running" ? html`<div class="process-controls">
          <label class="process-stdin"><span>stdin</span><textarea rows="2" value=${stdin} placeholder="Write to the running process…" onInput=${(event) => setStdin(event.currentTarget.value)}></textarea></label>
          <div class="button-row process-controls__actions">
            <button class="primary" disabled=${!stdin} onClick=${() => controller.sendProcessInput(selectedDetail.processID, stdin).then(() => setStdin(""))}>Write stdin</button>
            <button onClick=${() => controller.signalProcess(selectedDetail.processID, "interrupt")}>Interrupt</button>
            <button class="danger-text" onClick=${() => controller.signalProcess(selectedDetail.processID, "terminate")}>Terminate</button>
          </div>
        </div>` : null}
      ` : html`
        <div class="process-empty process-empty--detail">
          <div>
            <strong>${filter === "running" ? "No running process selected" : "Select a process"}</strong>
            <span>${filter === "running" && completedCount ? "Completed processes are hidden by the current filter." : "Choose a process from the sidebar to inspect its live output."}</span>
            ${filter === "running" && completedCount ? html`<button onClick=${showAll}>Show completed processes</button>` : null}
          </div>
        </div>
      `}
    </section>
  </div>`;
}

function statusGlyph(status) {
  if (status === "running") return "…";
  if (status === "failed") return "✗";
  return "✓";
}

function statusText(process) {
  if (process.status === "running") return "running";
  if (process.status === "failed") return process.exitCode == null ? "failed" : `failed · exit ${process.exitCode}`;
  return process.exitCode == null ? "exited" : `exited · ${process.exitCode}`;
}

function singleLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim() || "Command";
}

function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  if (minutes < 60) return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function outputSummary(output) {
  if (!output) return "No retained output";
  const retained = formatBytes(output.retainedBytes);
  if (output.truncated) return `${retained} retained · tail only`;
  return `${retained} retained`;
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(value >= 100 * 1024 ? 0 : 1)} KiB`;
  return `${value} B`;
}
