import { html, useEffect, useLayoutEffect, useRef, useState } from "../../../vendor/htm-preact.js";
import { controller } from "../../state/controller.js";
import { Feedback, LoadState, useActions } from "../config/feedback.js";
import { defaultProcessFilter, formatDuration, formatStarted, selectedProcessDetail, singleLine, statusText, visibleProcesses } from "./logic.js";
import { ProcessControls } from "./controls.js";
import { ProcessOutput } from "./output.js";

export function ProcessView({ sessionID, data, capabilities, inspector, preferences, patchPreferences }) {
  const processes = data?.processes;
  const [requestedID, setRequestedID] = useState(inspector?.processID || null);
  const [pollError, setPollError] = useState("");
  const listRef = useRef(null);
  const mainRef = useRef(null);
  const initialized = useRef(false);
  const { feedback, run } = useActions();
  const detail = selectedProcessDetail(data?.processDetail, requestedID);
  const filter = defaultProcessFilter(processes || [], preferences.filter);
  const visible = visibleProcesses(processes || [], filter, preferences.search);
  const runningCount = (processes || []).filter((process) => process.status === "running").length;
  const outsideFilter = detail && !visible.some((process) => process.processID === detail.processID);
  const select = (processID, showDetail = true) => {
    setRequestedID(processID);
    patchPreferences({ selectedID: processID, ...(showDetail ? { mobilePane: "detail" } : {}) });
    void run("selection", () => controller.loadProcess(processID), "", { replace: true });
  };

  useEffect(() => {
    if (!processes || initialized.current) return;
    initialized.current = true;
    if (!preferences.filter) patchPreferences({ filter });
    if (!data?.processDetail && !inspector?.processID && inspector?.runtimeSessionID == null) {
      const processID = preferences.selectedID || visible[0]?.processID;
      if (processID) select(processID, false);
    }
  }, [processes]);
  useEffect(() => {
    if (inspector?.processID) {
      setRequestedID(inspector.processID);
      patchPreferences({ selectedID: inspector.processID,
        ...(inspector.from || preferences.selectedID !== inspector.processID ? { mobilePane: "detail" } : {}),
      });
    }
  }, [inspector?.processID]);
  useEffect(() => {
    if (detail?.processID) patchPreferences({ selectedID: detail.processID });
  }, [detail?.processID]);
  useLayoutEffect(() => { if (listRef.current) listRef.current.scrollTop = preferences.scrollTop || 0; }, [Boolean(processes)]);
  useLayoutEffect(() => { if (mainRef.current) mainRef.current.scrollTop = preferences.mainScrollTop || 0; }, [Boolean(detail)]);
  useEffect(() => {
    setPollError("");
    if (!detail || detail.status !== "running") return undefined;
    let active = true;
    let outputBusy = false;
    let metadataBusy = false;
    const poll = async (metadata) => {
      if (metadata ? metadataBusy : outputBusy) return;
      if (metadata) metadataBusy = true; else outputBusy = true;
      try {
        await (metadata ? controller.refreshProcess(detail.processID) : controller.refreshProcessOutput(detail.processID));
      } catch (error) {
        if (active) setPollError(error?.message || String(error));
      } finally {
        if (metadata) metadataBusy = false; else outputBusy = false;
      }
    };
    const outputTimer = window.setInterval(() => { void poll(false); }, 180);
    const metadataTimer = window.setInterval(() => { void poll(true); }, 1000);
    return () => { active = false; window.clearInterval(outputTimer); window.clearInterval(metadataTimer); };
  }, [sessionID, detail?.processID, detail?.status]);

  const refresh = () => { void run("refresh", async () => {
    await Promise.all([controller.refreshProcesses(sessionID), detail ? controller.loadProcess(detail.processID) : Promise.resolve()]);
    setPollError("");
  }, "Refreshed"); };
  const row = (process) => html`<button key=${process.processID} class=${`process-row ${detail?.processID === process.processID ? "is-selected" : ""}`} aria-current=${detail?.processID === process.processID ? "true" : undefined} onClick=${() => select(process.processID)} title=${process.cwd}>
    <strong class="process-row__command">${singleLine(process.command)}</strong>
    <span class="process-row__identity"><span class=${`process-state process-state--${process.status}`}>${statusText(process)}</span><span>#${process.runtimeSessionID}</span><span>${formatDuration(process.elapsedMs)}</span></span>
    <time datetime=${process.startedAt} title=${process.startedAt}>Started ${formatStarted(process.startedAt)}</time>
  </button>`;
  if (!processes) return html`<${LoadState} error=${data?.inspectorErrors?.process} label="Loading managed processes…" />`;
  return html`<div class=${`process-inspector process-inspector--${preferences.mobilePane}`}>
    <aside class="process-sidebar">
      <div class="process-sidebar__header"><div><strong>Processes</strong><span>${runningCount} running / ${processes.length} retained</span></div><button disabled=${feedback.refresh?.pending} onClick=${refresh}>Refresh</button></div>
      <${Feedback} state=${feedback.refresh} pendingLabel="Refreshing…" />
      <input class="process-search" type="search" aria-label="Search processes" placeholder="Search commands, IDs or paths" value=${preferences.search} onInput=${(event) => patchPreferences({ search: event.currentTarget.value })} />
      <div class="process-filter" role="group" aria-label="Process filter">${["running", "all"].map((value) => html`<button aria-pressed=${filter === value} class=${filter === value ? "is-active" : ""} onClick=${() => patchPreferences({ filter: value })}>${value === "running" ? "Running" : "All / recent"}</button>`)}</div>
      <p class="process-order">Newest started first. Retained runtime processes only.</p>
      <div ref=${listRef} class="process-list" onScroll=${(event) => patchPreferences({ scrollTop: event.currentTarget.scrollTop })}>
        ${outsideFilter ? html`<div class="process-watched"><span>Selected process outside this filter</span>${row(detail)}</div>` : null}
        ${visible.map(row)}
        ${!visible.length ? html`<div class="process-empty"><strong>${preferences.search ? "No matching processes" : filter === "running" ? "No running processes" : "No retained processes"}</strong><span>${preferences.search ? "Try another command, ID or path." : "Managed background commands appear here."}</span>${filter === "running" ? html`<button onClick=${() => patchPreferences({ filter: "all" })}>Show all / recent</button>` : null}</div>` : null}
      </div>
    </aside>
    <section ref=${mainRef} class="process-main" onScroll=${(event) => patchPreferences({ mainScrollTop: event.currentTarget.scrollTop })}>
      <div class="process-back-row"><button class="process-mobile-back" onClick=${() => patchPreferences({ mobilePane: "list" })}>Back to processes</button>${inspector?.from ? html`<button onClick=${() => { void run("back", () => controller.backInspector(), ""); }}>Back to origin</button>` : null}<${Feedback} state=${feedback.back} /></div>
      <${Feedback} state=${feedback.selection} pendingLabel="Loading process…" />
      ${data?.processDetailError ? html`<p class="process-error" role="alert">${data.processDetailError}</p>` : null}
      ${detail ? html`<header class="process-detail-header"><h2>${singleLine(detail.command)}</h2><div class="process-detail-header__facts"><span class=${`process-state process-state--${detail.status}`}>${statusText(detail)}</span><span>Runtime #${detail.runtimeSessionID}</span><span>PID ${detail.pid ?? "not reported"}</span><span>Elapsed ${formatDuration(detail.elapsedMs)}</span><span>TTY ${detail.tty ? "yes" : "no"}</span></div><time datetime=${detail.startedAt}>Started ${formatStarted(detail.startedAt)}</time></header>
        <div class="process-location-line"><span>Working directory</span><code>${detail.cwd}</code></div>
        ${pollError ? html`<div class="process-error" role="alert">Output refresh failed: ${pollError}<button onClick=${refresh} disabled=${feedback.refresh?.pending}>Retry refresh</button></div>` : null}
        <${ProcessOutput} key=${detail.processID} process=${detail} preferences=${preferences} patchPreferences=${patchPreferences} />
        ${!capabilities?.features?.pty ? html`<p class="process-note">Text output and managed controls. PTY terminal emulation is not enabled by this server.</p>` : null}
        ${detail.status === "running" ? html`<${ProcessControls} key=${detail.processID} process=${detail} />` : null}
      ` : html`<div class="process-empty"><strong>${feedback.selection?.pending ? "Loading selected process…" : "Select a process"}</strong><span>Choose a command to inspect its retained output.</span>${requestedID && !feedback.selection?.pending ? html`<button onClick=${() => select(requestedID)}>Retry selected process</button>` : null}</div>`}
    </section>
  </div>`;
}
