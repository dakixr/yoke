import { html, useEffect, useLayoutEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { workingDuration } from "../lib/duration.js";
import { controller } from "../state/controller.js";
import { getScroll, setScroll } from "../state/local-state.js";
import { markdownHTML } from "./markdown.js";

export function Timeline({ sessionID, data, runtime }) {
  const viewport = useRef(null);
  const followingRef = useRef(true);
  const restore = useRef({ sessionID: null, target: 0, complete: false });
  const scrollOwner = useRef(sessionID);
  const suppressScroll = useRef(false);
  const [following, setFollowing] = useState(true);
  const messages = data?.messages || [];
  const livePrompt = data?.livePrompt;
  const failedPrompts = data?.failedPrompts || [];
  const liveAssistants = orderedLiveValues(data?.liveAssistants);
  const liveTools = orderedLiveValues(data?.liveTools);
  const liveToolsByID = Object.fromEntries(liveTools.map((tool) => [tool.callID, tool]));
  const persistedToolCalls = persistedToolCallMap(messages);
  const tailLiveTools = liveTools.filter((tool) => !persistedToolCalls.has(tool.callID));
  const liveTailItems = orderedLiveTail(liveAssistants, tailLiveTools);
  const liveAssistantSignature = liveAssistants.map((item) => `${item.id}:${item.content}`).join("|");
  const liveToolSignature = liveTools.map((item) => `${item.callID}:${item.status}`).join("|");

  if (restore.current.sessionID !== sessionID) {
    restore.current = { sessionID, target: getScroll(sessionID), complete: false };
    suppressScroll.current = true;
  }

  useLayoutEffect(() => {
    const node = viewport.current;
    if (!node) return;
    if (restore.current.complete || !data?.loaded) return;
    scrollOwner.current = sessionID;
    const target = restore.current.target;
    node.scrollTop = target > 0 ? Math.min(target, Math.max(0, node.scrollHeight - node.clientHeight)) : node.scrollHeight;
    const near = isNearBottom(node);
    followingRef.current = near;
    setFollowing(near);
    restore.current.complete = true;
    const frame = requestAnimationFrame(() => {
      if (scrollOwner.current === sessionID) suppressScroll.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [sessionID, data?.loaded, messages.length]);

  useEffect(() => {
    const node = viewport.current;
    if (!node || !followingRef.current || !restore.current.complete) return;
    const frame = requestAnimationFrame(() => {
      if (restore.current.sessionID !== sessionID || !followingRef.current) return;
      node.scrollTop = node.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [sessionID, messages.length, failedPrompts.length, livePrompt?.id, liveAssistantSignature, liveToolSignature, runtime?.state, runtime?.startedAt, runtime?.activity, following]);

  const onScroll = () => {
    const node = viewport.current;
    if (!node || suppressScroll.current) return;
    const near = isNearBottom(node);
    followingRef.current = near;
    setFollowing(near);
    setScroll(scrollOwner.current, node.scrollTop);
  };

  return html`
    <div class="timeline-wrap">
      <div class="timeline" ref=${viewport} onScroll=${onScroll} aria-label="Conversation timeline">
        <div class="timeline__inner">
          ${data?.messageCursor ? html`
            <button class="load-older" disabled=${data.loadingOlder} onClick=${() => controller.loadOlderMessages(sessionID)}>
              ${data.loadingOlder ? "Loading…" : "Load older turns"}
            </button>
          ` : null}
          ${messages.length ? messages.map((message) => html`<${TimelineMessage} key=${message.id} sessionID=${sessionID} message=${message} liveToolsByID=${liveToolsByID} toolNames=${persistedToolCalls} />`) : !failedPrompts.length && !livePrompt && !liveAssistants.length && !liveTools.length && !runtime?.activity ? html`
            <div class="timeline-empty">This session has no messages yet.</div>
          ` : null}
          ${failedPrompts.map((prompt) => html`<${UserMessage} key=${prompt.id} message=${livePromptMessage(prompt)} />`)}
          ${livePrompt ? html`<${UserMessage} message=${livePromptMessage(livePrompt)} />` : null}
          ${liveTailItems.map((item) => item.kind === "assistant"
            ? item.value.content ? html`<${AssistantMessage} key=${item.value.id} message=${{ phase: item.value.phase, content: [{ type: "text", text: item.value.content }] }} />` : null
            : html`<button key=${item.value.callID} class=${`tool-line tool-line--live tool-line--${item.value.status}`} onClick=${() => item.value.callID && controller.openInspector("tool", { callID: item.value.callID })}>
                <span class="tool-line__glyph">${toolGlyph(item.value.status)}</span>
                <span>${humanToolName(item.value.name)}</span>
                <span class="tool-line__state">${toolStatusLabel(item.value.status)}</span>
                ${item.value.arguments ? html`<code>${compactArguments(item.value.arguments)}</code>` : null}
              </button>`)}
          ${runtime?.activity && !liveTools.length ? html`<${ActivityIndicator} startedAt=${runtime.startedAt} activity=${runtime.activity} />` : null}
          ${data?.lastError ? html`<div class="timeline-error" role="status">${data.lastError}</div>` : null}
        </div>
      </div>
      ${!following ? html`<button class="jump-latest" onClick=${() => { followingRef.current = true; setFollowing(true); viewport.current?.scrollTo({ top: viewport.current.scrollHeight }); }}>Jump to latest ↓</button>` : null}
    </div>
  `;
}

function ActivityIndicator({ startedAt, activity }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => tick((value) => value + 1), 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);
  const duration = workingDuration(startedAt).replace("Working ", "");
  return html`
    <div class="chat-working" role="status" aria-label=${`Yoke status: ${activity}`}>
      <span class="chat-working__mark" aria-hidden="true"></span>
      <span class="chat-working__label">${activity}</span>
      <span class="chat-working__duration" aria-hidden="true">${duration}</span>
    </div>
  `;
}

function TimelineMessage({ sessionID, message, liveToolsByID, toolNames }) {
  if (message.type === "user") return html`<${UserMessage} message=${message} />`;
  if (message.type === "assistant") return html`<${AssistantMessage} sessionID=${sessionID} message=${message} liveToolsByID=${liveToolsByID} />`;
  if (message.type === "tool") return html`<${ToolMessage} sessionID=${sessionID} message=${message} toolName=${toolNames.get(message.callID) || null} />`;
  return html`<div class="control-line"><span>${message.control || "Control"}</span>${message.text ? html`<span>${message.text}</span>` : null}</div>`;
}

function UserMessage({ message }) {
  return html`<article class="turn turn--user">
    <div class="turn__rail"><span class="turn__label">You</span><time>${formatTime(message.timeCreated)}</time></div>
    <div class="turn__body user-content">
      ${message.content?.map((part, index) => part.type === "text"
        ? html`<div key=${index} class="plain-text">${part.text}</div>`
        : html`<span key=${index} class="attachment-chip">▧ ${part.name}</span>`)}
    </div>
  </article>`;
}

function livePromptMessage(livePrompt) {
  const prompt = livePrompt.prompt || {};
  return {
    id: livePrompt.id,
    timeCreated: livePrompt.timeCreated,
    content: [
      ...(prompt.text ? [{ type: "text", text: prompt.text }] : []),
      ...(prompt.attachments || []).map((attachment) => ({
        type: "image",
        name: attachment.name,
      })),
    ],
  };
}

function AssistantMessage({ sessionID = null, message, liveToolsByID = {} }) {
  const text = (message.content || []).filter((part) => part.type === "text").map((part) => part.text).join("\n");
  const images = (message.content || []).filter((part) => part.type === "image");
  const phase = message.phase === "commentary" ? "Commentary" : "Assistant";
  return html`<article class=${`turn turn--assistant ${message.phase === "commentary" ? "is-commentary" : ""}`}>
    <div class="turn__rail"><span class="turn__label">${phase}</span>${message.timeCreated ? html`<time>${formatTime(message.timeCreated)}</time>` : null}</div>
    <div class="turn__body assistant-content">
      ${text ? html`<div class="markdown" dangerouslySetInnerHTML=${{ __html: markdownHTML(text) }}></div>` : null}
      ${images.map((part) => html`<span class="attachment-chip">▧ ${part.name}</span>`)}
      ${message.toolCalls?.length ? html`<div class="tool-call-list">
        ${message.toolCalls.map((call) => html`
          <button key=${call.id} class=${`tool-line ${liveToolsByID[call.id] ? `tool-line--${liveToolsByID[call.id].status}` : ""}`} onClick=${() => sessionID && controller.openInspector("tool", { callID: call.id })}>
            <span class="tool-line__glyph">${liveToolsByID[call.id] ? toolGlyph(liveToolsByID[call.id].status) : "↳"}</span><span>${humanToolName(call.name)}</span><code>${compactArguments(call.arguments)}</code>
            ${liveToolsByID[call.id] ? html`<span class="tool-line__state">${toolStatusLabel(liveToolsByID[call.id].status)}</span>` : null}
          </button>
        `)}
      </div>` : null}
    </div>
  </article>`;
}

function ToolMessage({ sessionID, message, toolName = null }) {
  const summary = compactResult(message.result);
  return html`<div class="tool-result-row">
    <button class="tool-line" onClick=${() => message.callID && controller.openInspector("tool", { callID: message.callID })}>
      <span class="tool-line__glyph">✓</span><span>${toolName ? humanToolName(toolName) : "Tool"} completed</span>${summary ? html`<span class="tool-line__summary">${summary}</span>` : null}
    </button>
  </div>`;
}

function orderedLiveValues(values) {
  return Object.values(values || {}).sort((left, right) => {
    const sequence = (left.sequence || 0) - (right.sequence || 0);
    if (sequence) return sequence;
    return String(left.callID || left.id || "").localeCompare(String(right.callID || right.id || ""));
  });
}

function orderedLiveTail(assistants, tools) {
  return [
    ...assistants.map((value) => ({ kind: "assistant", value, time: value.timeCreated })),
    ...tools.map((value) => ({ kind: "tool", value, time: value.startedAt })),
  ].sort((left, right) => {
    const leftTime = Date.parse(left.time || "");
    const rightTime = Date.parse(right.time || "");
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) return leftTime - rightTime;
    return (left.value.sequence || 0) - (right.value.sequence || 0);
  });
}

function persistedToolCallMap(messages) {
  const calls = new Map();
  for (const message of messages) {
    if (message.type !== "assistant") continue;
    for (const call of message.toolCalls || []) calls.set(call.id, call.name);
  }
  return calls;
}

function toolStatusLabel(status) {
  if (status === "running") return "working";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  return "done";
}

function toolGlyph(status) {
  if (status === "running") return "↳";
  if (status === "failed") return "×";
  if (status === "cancelled") return "·";
  return "✓";
}

function compactArguments(raw) {
  if (!raw) return "";
  const oneLine = raw.replace(/\s+/g, " ").trim();
  return oneLine.length > 90 ? `${oneLine.slice(0, 87)}…` : oneLine;
}

function compactResult(raw) {
  if (!raw) return "";
  const oneLine = raw.replace(/\s+/g, " ").trim();
  return oneLine.length > 120 ? `${oneLine.slice(0, 117)}…` : oneLine;
}

function humanToolName(name) {
  return String(name || "Tool").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isNearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 120;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
