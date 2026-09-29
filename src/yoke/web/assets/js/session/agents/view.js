import { html, useLayoutEffect, useMemo, useState } from "../../../vendor/htm-preact.js";
import { api } from "../../api/client.js";
import { store } from "../../state/store.js";
import { AgentRosterController } from "./controller.js";
import { agentPresentation, agentText, INACTIVE_AGENT_LIMIT, liveAgent } from "./model.js";

export function AgentRoster() {
  const controller = useMemo(() => new AgentRosterController({ api, store }), []);
  const [state, setState] = useState(controller.getState());
  useLayoutEffect(() => {
    const unsubscribe = controller.subscribe(() => setState(controller.getState()));
    controller.start();
    return () => { unsubscribe(); controller.stop(); };
  }, [controller]);
  return html`<${AgentRosterView} state=${state} retry=${() => controller.refresh()} />`;
}

export function AgentRosterView({ state, retry }) {
  if (!state.sessionID) return null;
  const active = state.current ? state.rows.filter(liveAgent).length : 0;
  const count = state.current ? `${active} active` : "Active count unavailable";
  return html`<details class="agent-roster" open key=${state.sessionID}>
    <summary class="agent-roster__summary">
      <span>Agents</span>
      <span class=${`agent-roster__count ${active ? "is-active" : ""}`} role="status" aria-live="polite" aria-atomic="true">${count}</span>
      ${state.rows.length ? html`<span class="muted">${state.rows.length} shown</span>` : null}
    </summary>
    <div class="agent-roster__body" tabIndex="0" role="region" aria-label="Agent roster">
      ${state.error ? html`<div class="agent-roster__notice" role="status"><span>${state.error}</span><button class="header-action" onClick=${retry}>Retry</button></div>`
        : !state.current ? html`<p class="agent-roster__notice">${state.loading ? "Waiting for a current agent snapshot…" : "Agent state is not current. Waiting for connection…"}</p>` : null}
      ${!state.rows.length && state.current ? html`<p class="agent-roster__notice">No agents registered in this session.</p>` : null}
      ${state.rows.length ? html`<ul class="agent-roster__list" aria-label="Session agents">
        ${state.rows.map((run) => html`<${AgentRow} key=${run.agentId} run=${run} current=${state.current} />`)}
      </ul>` : null}
      ${state.rows.filter((run) => !liveAgent(run)).length >= INACTIVE_AGENT_LIMIT ? html`<p class="agent-roster__notice">Showing the ${INACTIVE_AGENT_LIMIT} most recent inactive agents and all live agents.</p>` : null}
    </div>
  </details>`;
}

function AgentRow({ run, current }) {
  const status = agentPresentation(run, current);
  const model = [agentText(run.provider), agentText(run.model)].filter(Boolean).join(" / ");
  const name = agentText(run.name, model || "Unnamed agent");
  return html`<li class="agent-roster__row">
    <div class="agent-roster__identity"><strong>${name}</strong><span class="muted">${model || "Model unavailable"}</span></div>
    <div class=${`agent-roster__status agent-roster__status--${status.tone}`}>
      <span>${status.label}</span>
      ${status.detail ? html`<small class="muted">${status.detail}</small>` : null}
      ${agentText(run.errorType) ? html`<small>${run.errorType}</small>` : null}
    </div>
    <div class="agent-roster__tool"><span class="muted">Last tool</span><code>${agentText(run.lastToolName, "None reported")}</code></div>
  </li>`;
}
