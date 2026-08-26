import { html, useEffect, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function QueueEditor({ sessionID, queue }) {
  if (!queue?.items?.length) return null;
  return html`<section class="queue-editor" aria-label="Pending work">
    <div class="queue-editor__heading"><span>Pending work</span><span class="muted" title=${`Queue revision ${queue.revision}`}>${queue.items.length} pending</span></div>
    <div class="queue-editor__items">
      ${queue.items.map((item, index) => html`<${QueueItem} key=${item.id} sessionID=${sessionID} queue=${queue} item=${item} index=${index} />`)}
    </div>
  </section>`;
}

function QueueItem({ sessionID, queue, item, index }) {
  const [text, setText] = useState(item.prompt?.text || "");
  useEffect(() => setText(item.prompt?.text || ""), [item.id, item.prompt?.text]);
  const save = () => controller.patchQueue(sessionID, [{ op: "update", id: item.id, prompt: { ...item.prompt, text } }]);
  const moveUp = () => index > 0 && controller.patchQueue(sessionID, [{ op: "moveBefore", id: item.id, beforeID: queue.items[index - 1].id }]);
  const moveDown = () => index < queue.items.length - 1 && controller.patchQueue(sessionID, [{ op: "moveAfter", id: item.id, afterID: queue.items[index + 1].id }]);
  return html`<div class=${`queue-item ${item.paused ? "is-paused" : ""}`}>
    <div class="queue-item__index">${index + 1}</div>
    <div class="queue-item__body">
      <textarea rows="1" value=${text} aria-label="Queued prompt" onInput=${(event) => setText(event.currentTarget.value)} onBlur=${() => text !== item.prompt?.text && save()}></textarea>
      ${item.prompt?.attachments?.length ? html`<div class="attachment-row">${item.prompt.attachments.map((attachment) => html`<span class="attachment-chip">▧ ${attachment.name}</span>`)}</div>` : null}
      <div class="queue-item__controls">
        <select value=${item.delivery} aria-label="Queue delivery" onChange=${(event) => controller.patchQueue(sessionID, [{ op: "setDelivery", id: item.id, delivery: event.currentTarget.value }])}>
          <option value="steer">Steer now</option><option value="queue">Queue next</option>
        </select>
        <button onClick=${() => controller.patchQueue(sessionID, [{ op: "setPaused", id: item.id, paused: !item.paused }])}>${item.paused ? "Resume" : "Pause"}</button>
        <button disabled=${index === 0} title="Move to start" onClick=${() => controller.patchQueue(sessionID, [{ op: "moveToStart", id: item.id }])}>To start</button>
        <button disabled=${index === 0} aria-label="Move up" title="Move up" onClick=${moveUp}>↑</button>
        <button disabled=${index === queue.items.length - 1} aria-label="Move down" title="Move down" onClick=${moveDown}>↓</button>
        <button class="danger-text" onClick=${() => controller.patchQueue(sessionID, [{ op: "remove", id: item.id }])}>Remove</button>
      </div>
    </div>
  </div>`;
}
