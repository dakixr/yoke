import { html, useLayoutEffect, useRef } from "../../../vendor/htm-preact.js";
import { LoadState } from "../config/feedback.js";
import { formatDuration, formatStarted } from "../process/logic.js";
import { agentPresentation, agentText, INACTIVE_AGENT_LIMIT, liveAgent } from "./model.js";
import { activeAgentCount, agentChildren, agentDurationMs, agentModel, agentName, defaultAgentFilter, visibleAgents } from "./logic.js";

// Pure rendering of one current roster. The owner supplies time, so tests and
// finished rows never schedule timers.
export function AgentsView({ state, preferences, patchPreferences, now = Date.now(), retry, openProcess = null, backToOrigin = null }) {
  const listRef = useRef(null);
  useLayoutEffect(() => { if (listRef.current) listRef.current.scrollTop = preferences.scrollTop || 0; }, [state.loaded]);
  if (!state.sessionID) return null;
  if (!state.loaded) {
    if (state.error) return html`<div class="agent-notice" role="alert"><span>${state.error}</span><button onClick=${retry}>Retry</button></div>`;
    return html`<${LoadState} label=${state.loading ? "Loading agents…" : "Agent state is not current. Waiting for connection…"} />`;
  }
  const rows = state.rows;
  const filter = defaultAgentFilter(rows, preferences.filter);
  const visible = visibleAgents(rows, filter, preferences.search);
  const selected = rows.find((run) => run.agentId === preferences.selectedID)
    || (preferences.selectedID ? null : visible[0]?.run || null);
  const outsideFilter = selected && !visible.some(({ run }) => run.agentId === selected.agentId);
  const active = activeAgentCount(state);
  const select = (agentId) => patchPreferences({ selectedID: agentId, mobilePane: "detail" });
  const row = (run, depth = 0) => {
    const status = agentPresentation(run, state.current);
    const isSelected = selected?.agentId === run.agentId;
    return html`<button key=${run.agentId} class=${`agent-row ${isSelected ? "is-selected" : ""}`} style=${`--agent-depth: ${depth}`} aria-current=${isSelected ? "true" : undefined} onClick=${() => select(run.agentId)}>
      <strong class="agent-row__name">${agentName(run)}</strong>
      <span class="agent-row__facts"><span class=${`agent-state agent-state--${status.tone}`}>${status.label}</span><span>${agentModel(run) || "Model unavailable"}</span><span>${formatDuration(agentDurationMs(run, now))}</span></span>
      <time datetime=${run.startedAt} title=${run.startedAt}>Started ${formatStarted(run.startedAt)}</time>
    </button>`;
  };
  return html`<div class=${`agent-inspector agent-inspector--${preferences.mobilePane}`}>
    <aside class="agent-sidebar">
      <div class="agent-sidebar__header"><div><strong>Agents</strong><span role="status" aria-live="polite" aria-atomic="true">${active == null ? "Running count unavailable" : `${active} running`} / ${rows.length} retained</span></div><button disabled=${state.loading} onClick=${retry}>Refresh</button></div>
      ${state.error ? html`<div class="agent-notice" role="status"><span>${state.error}</span><button onClick=${retry}>Retry</button></div>`
        : !state.current ? html`<p class="agent-notice">Agent state is not current. Waiting for connection…</p>` : null}
      <input class="agent-search" type="search" aria-label="Search agents" placeholder="Search names, models, tools or IDs" value=${preferences.search} onInput=${(event) => patchPreferences({ search: event.currentTarget.value })} />
      <div class="agent-filter" role="group" aria-label="Agent filter">${["running", "all"].map((value) => html`<button key=${value} aria-pressed=${filter === value} class=${filter === value ? "is-active" : ""} onClick=${() => patchPreferences({ filter: value })}>${value === "running" ? "Running" : "All / recent"}</button>`)}</div>
      <p class="agent-order">Running first, then newest started. Sub-agents appear under their parent.</p>
      <div ref=${listRef} class="agent-list" aria-label="Session agents" onScroll=${(event) => patchPreferences({ scrollTop: event.currentTarget.scrollTop })}>
        ${outsideFilter ? html`<div class="agent-watched"><span>Selected agent outside this filter</span>${row(selected)}</div>` : null}
        ${visible.map(({ run, depth }) => row(run, depth))}
        ${!visible.length ? html`<div class="agent-empty"><strong>${preferences.search ? "No matching agents" : filter === "running" ? "No running agents" : "No agents registered in this session"}</strong><span>${preferences.search ? "Try another name, model or ID." : "Agents started through the Yoke SDK appear here."}</span>${filter === "running" && rows.length ? html`<button onClick=${() => patchPreferences({ filter: "all" })}>Show all / recent</button>` : null}</div>` : null}
        ${rows.filter((run) => !liveAgent(run)).length >= INACTIVE_AGENT_LIMIT ? html`<p class="agent-order">Showing the ${INACTIVE_AGENT_LIMIT} most recent inactive agents and all live agents.</p>` : null}
      </div>
    </aside>
    <section class="agent-main">
      <div class=${`agent-back-row ${backToOrigin ? "has-origin" : ""}`}><button class="agent-mobile-back" onClick=${() => patchPreferences({ mobilePane: "list" })}>Back to agents</button>${backToOrigin ? html`<button onClick=${backToOrigin}>Back to origin</button>` : null}</div>
      ${selected ? html`<${AgentDetail} run=${selected} rows=${rows} current=${state.current} now=${now} select=${select} openProcess=${openProcess} />`
        : html`<div class="agent-empty"><strong>${preferences.selectedID ? "Agent no longer retained" : "Select an agent"}</strong><span>${preferences.selectedID ? "The server no longer reports this agent." : "Choose an agent to inspect its run."}</span></div>`}
    </section>
  </div>`;
}

function AgentDetail({ run, rows, current, now, select, openProcess }) {
  const status = agentPresentation(run, current);
  const parent = run.parentAgentId ? rows.find((row) => row.agentId === run.parentAgentId) : null;
  const children = agentChildren(rows, run.agentId);
  const link = (target) => html`<button key=${target.agentId} class="agent-link" onClick=${() => select(target.agentId)}>${agentName(target)}</button>`;
  const facts = [
    ["Model", agentModel(run) || "Not reported"],
    agentText(run.effort) ? ["Effort", run.effort] : null,
    ["Started", html`<time datetime=${run.startedAt}>${formatStarted(run.startedAt)}</time>`],
    ["Finished", run.finishedAt ? html`<time datetime=${run.finishedAt}>${formatStarted(run.finishedAt)}</time>` : run.status === "running" ? "Not finished" : "Not reported"],
    ["Duration", formatDuration(agentDurationMs(run, now))],
    ["Last tool", agentText(run.lastToolName) ? html`<code>${run.lastToolName}</code>` : "None reported"],
    agentText(run.errorType) ? ["Error", html`<code>${run.errorType}</code>`] : null,
    run.attempt != null ? ["Attempt", String(run.attempt)] : null,
    agentText(run.taskId) ? ["Task", html`<code>${run.taskId}</code>`] : null,
    run.parentAgentId ? ["Parent", parent ? link(parent) : html`<code>${run.parentAgentId}</code>`] : null,
    children.length ? ["Sub-agents", html`<span class="agent-links">${children.map(link)}</span>`] : null,
    run.runtimeSessionID != null ? ["Process", html`<span class="agent-links"><span>Runtime #${run.runtimeSessionID}</span>${openProcess ? html`<button class="agent-link" onClick=${() => openProcess(run)}>Open process</button>` : null}</span>`] : null,
    ["Agent ID", html`<code>${run.agentId}</code>`],
    ["Run ID", html`<code>${run.runId}</code>`],
  ].filter(Boolean);
  return html`<header class="agent-detail-header">
      <h2>${agentName(run)}</h2>
      <div class="agent-detail-header__facts"><span class=${`agent-state agent-state--${status.tone}`}>${status.label}</span>${status.detail ? html`<span>${status.detail}</span>` : null}</div>
    </header>
    <dl class="agent-facts">${facts.map(([label, value]) => html`<div key=${label}><dt>${label}</dt><dd>${value}</dd></div>`)}</dl>`;
}
