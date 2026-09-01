import { html, useEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { sortToolCallsChronologically } from "./tool-logic.js";

export function ToolInspector({ sessionID, inspector, data }) {
  const calls = data?.toolCalls;
  const detail = data?.toolDetail;
  const [search, setSearch] = useState("");
  const [raw, setRaw] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const listRef = useRef(null);
  const followingRef = useRef(true);
  const initializedScrollRef = useRef(false);

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    const sorted = sortToolCallsChronologically(calls);
    if (!query) return sorted;
    return sorted.filter((call) => toolSearchText(call).includes(query));
  }, [calls, search]);

  useEffect(() => {
    followingRef.current = true;
    initializedScrollRef.current = false;
  }, [sessionID, search]);

  useEffect(() => {
    const list = listRef.current;
    if (!list || !visible.length) return undefined;
    const frame = requestAnimationFrame(() => {
      if (!initializedScrollRef.current || followingRef.current) {
        list.scrollTop = list.scrollHeight;
      }
      initializedScrollRef.current = true;
    });
    return () => cancelAnimationFrame(frame);
  }, [sessionID, visible.length, search]);

  useEffect(() => {
    const requested = inspector.callID || null;
    if (requested) {
      if (detail?.id !== requested) {
        void controller.loadToolCall(sessionID, requested).catch((error) => controller.notice(error?.message || String(error)));
      }
      return;
    }
    if (detail) return;
    if (!calls?.length) return;
    const newest = visible.at(-1) || sortToolCallsChronologically(calls).at(-1);
    if (newest) void controller.selectToolCall(sessionID, newest.id).catch((error) => controller.notice(error?.message || String(error)));
  }, [sessionID, inspector.callID, calls, detail?.id]);

  useEffect(() => {
    if (!detail?.id || !["running", "pending"].includes(detail.status)) return;
    const timer = window.setInterval(() => {
      void controller.loadToolCall(sessionID, detail.id).catch(() => {});
    }, 600);
    return () => window.clearInterval(timer);
  }, [sessionID, detail?.id, detail?.status]);

  if (!calls) return html`<div class="inspector-loading">Loading tool activity…</div>`;

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await controller.listToolCalls(sessionID);
      if (detail?.id) await controller.loadToolCall(sessionID, detail.id);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setRefreshing(false);
    }
  };

  return html`<div class="tool-inspector-split">
    <aside class="tool-sidebar">
      <div class="tool-sidebar__header">
        <div>
          <strong>Tool calls</strong>
          <span>${calls.length} retained${visible.length !== calls.length ? ` · ${visible.length} matching` : ""}</span>
        </div>
        <button class="tool-refresh" disabled=${refreshing} title="Refresh tool activity" onClick=${refresh}>
          ${refreshing ? html`<span class="pending-spinner" aria-hidden="true"></span>` : html`<span aria-hidden="true">↻</span>`}
        </button>
      </div>
      <label class="tool-search">
        <span aria-hidden="true">⌕</span>
        <input value=${search} placeholder="Search calls" aria-label="Search tool calls" onInput=${(event) => setSearch(event.currentTarget.value)} />
        ${search ? html`<button aria-label="Clear tool search" onClick=${() => setSearch("")}>×</button>` : null}
      </label>
      <div
        class="tool-call-sidebar-list"
        role="list"
        aria-label="Tool calls"
        ref=${listRef}
        onScroll=${(event) => {
          const list = event.currentTarget;
          const remaining = list.scrollHeight - list.clientHeight - list.scrollTop;
          followingRef.current = remaining <= 36;
        }}
      >
        ${visible.map((call) => html`<button
          key=${call.id}
          role="listitem"
          class=${`tool-sidebar-row ${inspector.callID === call.id ? "is-selected" : ""}`}
          onClick=${() => controller.selectToolCall(sessionID, call.id)}
        >
          <span class=${`tool-sidebar-row__glyph tool-sidebar-row__glyph--${call.status}`} aria-hidden="true">${statusGlyph(call.status)}</span>
          <span class="tool-sidebar-row__main">
            <span class="tool-sidebar-row__topline"><strong>${call.toolName}</strong><span>${formatDuration(call.time?.durationMs)}</span></span>
            <span class="tool-sidebar-row__summary">${argumentSummary(call.arguments?.raw) || call.id}</span>
          </span>
          <span class="tool-sidebar-row__status">${statusLabel(call.status)}</span>
        </button>`)}
        ${!visible.length ? html`<div class="tool-sidebar-empty">${calls.length ? "No tool calls match this search." : "No tool calls yet."}</div>` : null}
      </div>
    </aside>

    <section class="tool-detail-pane">
      ${detail ? html`<${ToolDetail} detail=${detail} raw=${raw} wrap=${wrap} setRaw=${setRaw} setWrap=${setWrap} />` : inspector.callID ? html`
        <div class="tool-detail-empty"><div><strong>Loading tool call…</strong><span>${inspector.callID}</span></div></div>
      ` : html`
        <div class="tool-detail-empty"><div><strong>No tool call selected</strong><span>Select a call from the sidebar to inspect arguments, output, and context.</span></div></div>
      `}
    </section>
  </div>`;
}

function ToolDetail({ detail, raw, wrap, setRaw, setWrap }) {
  const filePath = typeof detail.arguments?.executed?.path === "string" ? detail.arguments.executed.path : null;
  const outputText = (detail.outputChunks || []).map((chunk) => chunk.text).join("");
  const running = detail.status === "running" || detail.status === "pending";
  const context = [...(detail.context || []), ...(detail.afterContext || [])];
  if (raw) {
    return html`<div class="tool-detail-document">
      <${ToolDetailHeader} detail=${detail} filePath=${filePath} raw=${raw} wrap=${wrap} setRaw=${setRaw} setWrap=${setWrap} />
      <pre class=${`tool-raw-document ${wrap ? "is-wrapped" : ""}`}>${JSON.stringify(detail, null, 2)}</pre>
    </div>`;
  }
  return html`<div class="tool-detail-document">
    <${ToolDetailHeader} detail=${detail} filePath=${filePath} raw=${raw} wrap=${wrap} setRaw=${setRaw} setWrap=${setWrap} />

    ${outputText || running ? html`<section class="tool-detail-output">
      <div class="tool-detail-section-head">
        <span>Output</span>
        <span>${running ? html`<span class="tool-live-dot"></span>LIVE · ` : ""}${detail.output?.retainedChars || outputText.length} chars${detail.output?.truncated ? " · truncated" : ""}</span>
      </div>
      <pre class=${wrap ? "is-wrapped" : ""}>${outputText || "Waiting for output…"}</pre>
    </section>` : null}

    <div class="tool-detail-grid">
      <${ToolDetailSection} title="Arguments" value=${detail.arguments?.raw || detail.arguments?.executed} wrap=${wrap} />
      ${detail.arguments?.executed ? html`<${ToolDetailSection} title="Executed arguments" value=${detail.arguments.executed} wrap=${wrap} />` : null}
      <${ToolDetailSection} title="Result" value=${detail.result} wrap=${wrap} />
    </div>

    ${context.length ? html`<section class="tool-context-document">
      <div class="tool-detail-section-head"><span>Context</span><span>${context.length} messages</span></div>
      <div class="tool-context-list">${context.map((item, index) => html`
        <div key=${index} class="tool-context-row">
          <span>${item.role === "assistant" ? "asst" : "usr"}</span>
          <p>${item.text || "(empty)"}</p>
        </div>`)}
      </div>
    </section>` : null}
  </div>`;
}

function ToolDetailHeader({ detail, filePath, raw, wrap, setRaw, setWrap }) {
  return html`<header class="tool-detail-header">
    <div class="tool-detail-heading">
      <span class=${`tool-detail-status-dot tool-detail-status-dot--${detail.status}`} aria-hidden="true"></span>
      <div>
        <span class="tool-detail-eyebrow">${detail.id}${detail.turnID != null ? ` · turn ${detail.turnID}` : ""}${detail.iteration != null ? ` · iteration ${detail.iteration}` : ""}</span>
        <h2>${detail.toolName}</h2>
      </div>
    </div>
    <div class="tool-detail-actions">
      <span class=${`status-pill status-pill--${detail.status}`}>${statusLabel(detail.status)}</span>
      <button class=${raw ? "is-active" : ""} aria-pressed=${raw} onClick=${() => setRaw((value) => !value)}>${raw ? "Pretty" : "Raw"}</button>
      <button class=${wrap ? "is-active" : ""} aria-pressed=${wrap} onClick=${() => setWrap((value) => !value)}>${wrap ? "Wrap on" : "Wrap off"}</button>
      ${filePath ? html`<button onClick=${() => controller.openInspector("file", { path: filePath })}>Open file</button>` : null}
    </div>
    <div class="tool-detail-meta">
      <span><b>Started</b>${formatDateTime(detail.time?.started)}</span>
      <span><b>Duration</b>${formatDuration(detail.time?.durationMs)}</span>
      <span><b>Retention</b>${detail.retention || "—"}</span>
    </div>
  </header>`;
}

function ToolDetailSection({ title, value, wrap }) {
  if (value == null || value === "") return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return html`<section class="tool-detail-section">
    <div class="tool-detail-section-head"><span>${title}</span></div>
    <pre class=${wrap ? "is-wrapped" : ""}>${text}</pre>
  </section>`;
}

function toolSearchText(call) {
  return [call.toolName, call.id, call.status, call.arguments?.raw, call.arguments?.executed, call.result]
    .filter((value) => value != null)
    .map((value) => typeof value === "string" ? value : JSON.stringify(value))
    .join(" ")
    .toLowerCase();
}

function statusGlyph(status) {
  if (status === "running" || status === "pending") return "…";
  if (status === "failed" || status === "cancelled") return "×";
  return "✓";
}

function statusLabel(status) {
  if (status === "ok") return "completed";
  return status || "unknown";
}

function argumentSummary(raw) {
  if (!raw) return "";
  const one = String(raw).replace(/\s+/g, " ").trim();
  return one.length > 86 ? `${one.slice(0, 83)}…` : one;
}

function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}
