import { html } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function ToolInspector({ sessionID, inspector, data }) {
  const detail = data?.toolDetail;
  if (inspector.callID) {
    if (!detail || detail.id !== inspector.callID) return html`<div class="inspector-loading">Loading tool call…</div>`;
    const filePath = typeof detail.arguments?.executed?.path === "string" ? detail.arguments.executed.path : null;
    return html`<div class="inspector-stack">
      <div class="inspector-title-row"><div><h2>${detail.toolName}</h2><span class=${`status-pill status-pill--${detail.status}`}>${detail.status}</span></div>${filePath ? html`<button onClick=${() => controller.openInspector("file", { path: filePath })}>Open file</button>` : null}</div>
      <dl class="detail-grid"><dt>Started</dt><dd title=${detail.time?.started || ""}>${formatDateTime(detail.time?.started)}</dd><dt>Ended</dt><dd title=${detail.time?.ended || ""}>${formatDateTime(detail.time?.ended)}</dd><dt>Duration</dt><dd>${detail.time?.durationMs != null ? `${detail.time.durationMs} ms` : "—"}</dd><dt>Retention</dt><dd>${detail.retention}</dd></dl>
      <${JsonSection} title="Arguments" value=${detail.arguments?.raw || detail.arguments?.executed} />
      ${detail.arguments?.executed ? html`<${JsonSection} title="Executed arguments" value=${detail.arguments.executed} />` : null}
      <${JsonSection} title="Result" value=${detail.result} />
      ${detail.context?.length ? html`<section><div class="inspector-section-title">Context</div>${detail.context.map((item) => html`<div class="context-line"><span>${item.role}</span><p>${item.text}</p></div>`)}</section>` : null}
      <section><div class="inspector-section-title">Retained output <span class="muted">${detail.output?.retainedChars || 0} chars${detail.output?.truncated ? " · truncated" : ""}</span></div><pre class="output-pre">${(detail.outputChunks || []).map((chunk) => chunk.text).join("") || "No retained output."}</pre></section>
    </div>`;
  }
  const calls = data?.toolCalls;
  if (!calls) return html`<div class="inspector-loading">Loading tool activity…</div>`;
  return html`<div class="inspector-stack"><div class="inspector-meta"><span>${calls.length} recent calls</span><button onClick=${() => controller.listToolCalls(sessionID)}>Refresh</button></div><div class="activity-list">
    ${calls.map((call) => html`<button class="activity-row" onClick=${() => controller.openInspector("tool", { callID: call.id })}><span class="activity-row__main"><strong>${call.toolName}</strong><span>${argumentSummary(call.arguments?.raw)}</span></span><span class=${`status-pill status-pill--${call.status}`}>${call.status}</span></button>`)}
  </div></div>`;
}

function JsonSection({ title, value }) {
  if (value == null || value === "") return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return html`<section><div class="inspector-section-title">${title}</div><pre>${text}</pre></section>`;
}

function argumentSummary(raw) {
  if (!raw) return "";
  const one = raw.replace(/\s+/g, " ");
  return one.length > 72 ? `${one.slice(0, 69)}…` : one;
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}
