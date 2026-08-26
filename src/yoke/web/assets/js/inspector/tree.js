import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function TreeInspector({ sessionID, data }) {
  const tree = data?.tree;
  const preview = data?.treePreview;
  const [summary, setSummary] = useState("");
  if (!tree) return html`<div class="inspector-loading">Loading tree…</div>`;
  return html`<div class="inspector-stack">
    <div class="inspector-meta"><span title=${`Tree revision ${tree.revision}`}>Current tree</span><span>${tree.entries.length} nodes</span></div>
    <div class="tree-list">
      ${tree.entries.map((entry) => html`<div class=${`tree-entry ${entry.current ? "is-current" : ""} ${entry.active ? "is-active" : "is-abandoned"}`}>
        <div class="tree-entry__line"><span class="tree-state">${entry.current ? "●" : entry.active ? "○" : "×"}</span><span class="tree-kind">${entry.kind}</span>${entry.label ? html`<span class="tree-label">${entry.label}</span>` : null}</div>
        ${entry.preview ? html`<div class="tree-preview-text">${entry.preview}</div>` : null}
        <div class="tree-entry__actions">
          ${entry.current ? html`<span class="tree-action-unavailable">Current node</span>` : html`<button onClick=${() => controller.treePreview(sessionID, entry.id)}>Preview</button>`}
          <button onClick=${() => { const label = window.prompt("Node label", entry.label || ""); if (label !== null) void controller.labelTreeEntry(sessionID, entry.id, label); }}>Edit label</button>
        </div>
      </div>`)}
    </div>
    ${preview ? html`<div class="navigation-preview">
      <div class="inspector-section-title">Navigation preview</div>
      ${preview.editorText ? html`<div><span class="field-label">Restored editor text</span><pre>${preview.editorText}</pre></div>` : null}
      ${preview.abandoned?.length ? html`<div><span class="field-label">Work that becomes abandoned</span><ul>${preview.abandoned.map((item) => html`<li><strong>${item.kind}</strong> ${item.preview || item.id}</li>`)}</ul></div>` : html`<div class="muted">No active work is abandoned.</div>`}
      <label class="stacked-label">Branch summary, optional<textarea rows="3" value=${summary} onInput=${(event) => setSummary(event.currentTarget.value)}></textarea></label>
      <button class="primary" disabled=${preview.current} onClick=${() => controller.navigateTree(sessionID, preview.targetID, summary || null)}>Navigate</button>
    </div>` : null}
  </div>`;
}
