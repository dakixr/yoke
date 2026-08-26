import { html, useEffect, useLayoutEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { getScroll, setScroll } from "../state/local-state.js";
import { markdownHTML } from "./markdown.js";

export function Timeline({ sessionID, data }) {
  const viewport = useRef(null);
  const followingRef = useRef(true);
  const restore = useRef({ sessionID: null, target: 0, complete: false });
  const scrollOwner = useRef(sessionID);
  const suppressScroll = useRef(false);
  const [following, setFollowing] = useState(true);
  const messages = data?.messages || [];
  const livePrompt = data?.livePrompt;
  const liveAssistant = data?.liveAssistant;
  const liveTool = data?.liveTool;

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
  }, [sessionID, messages.length, livePrompt?.id, liveAssistant?.content, liveTool?.callID, following]);

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
          ${messages.length ? messages.map((message) => html`<${TimelineMessage} key=${message.id} sessionID=${sessionID} message=${message} />`) : !livePrompt && !liveAssistant?.content && !liveTool ? html`
            <div class="timeline-empty">This session has no messages yet.</div>
          ` : null}
          ${livePrompt ? html`<${UserMessage} message=${livePromptMessage(livePrompt)} />` : null}
          ${liveAssistant?.content ? html`<${AssistantMessage} message=${{ phase: liveAssistant.phase, content: [{ type: "text", text: liveAssistant.content }] }} streaming />` : null}
          ${liveTool ? html`
            <button class="tool-line tool-line--live" onClick=${() => liveTool.callID && controller.openInspector("tool", { callID: liveTool.callID })}>
              <span class="tool-line__glyph">↳</span><span>Running ${humanToolName(liveTool.name)}</span><span class="muted">working</span>
            </button>
          ` : null}
          ${data?.lastError ? html`<div class="timeline-error" role="status">${data.lastError}</div>` : null}
        </div>
      </div>
      ${!following ? html`<button class="jump-latest" onClick=${() => { followingRef.current = true; setFollowing(true); viewport.current?.scrollTo({ top: viewport.current.scrollHeight }); }}>Jump to latest ↓</button>` : null}
    </div>
  `;
}

function TimelineMessage({ sessionID, message }) {
  if (message.type === "user") return html`<${UserMessage} message=${message} />`;
  if (message.type === "assistant") return html`<${AssistantMessage} sessionID=${sessionID} message=${message} />`;
  if (message.type === "tool") return html`<${ToolMessage} sessionID=${sessionID} message=${message} />`;
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

function AssistantMessage({ sessionID = null, message, streaming = false }) {
  const text = (message.content || []).filter((part) => part.type === "text").map((part) => part.text).join("\n");
  const images = (message.content || []).filter((part) => part.type === "image");
  const phase = message.phase === "commentary" ? "Commentary" : "Assistant";
  return html`<article class=${`turn turn--assistant ${message.phase === "commentary" ? "is-commentary" : ""}`}>
    <div class="turn__rail"><span class="turn__label">${phase}</span>${streaming ? html`<span class="turn__activity">writing</span>` : null}${message.timeCreated ? html`<time>${formatTime(message.timeCreated)}</time>` : null}</div>
    <div class="turn__body assistant-content">
      ${text ? html`<div class="markdown" dangerouslySetInnerHTML=${{ __html: markdownHTML(text) }}></div>` : null}
      ${images.map((part) => html`<span class="attachment-chip">▧ ${part.name}</span>`)}
      ${message.toolCalls?.length ? html`<div class="tool-call-list">
        ${message.toolCalls.map((call) => html`
          <button class="tool-line" onClick=${() => sessionID && controller.openInspector("tool", { callID: call.id })}>
            <span class="tool-line__glyph">↳</span><span>${humanToolName(call.name)}</span><code>${compactArguments(call.arguments)}</code>
          </button>
        `)}
      </div>` : null}
    </div>
  </article>`;
}

function ToolMessage({ sessionID, message }) {
  const summary = compactResult(message.result);
  return html`<div class="tool-result-row">
    <button class="tool-line" onClick=${() => message.callID && controller.openInspector("tool", { callID: message.callID })}>
      <span class="tool-line__glyph">✓</span><span>Tool completed</span>${summary ? html`<span class="tool-line__summary">${summary}</span>` : null}
    </button>
  </div>`;
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
