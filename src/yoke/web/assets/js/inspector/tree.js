import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function TreeInspector({ sessionID, data }) {
  const tree = data?.tree;
  const preview = data?.treePreview;
  const [summary, setSummary] = useState("");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [previewingID, setPreviewingID] = useState(null);
  const [navigating, setNavigating] = useState(false);
  const [showTechnical, setShowTechnical] = useState(false);

  if (!tree) return html`<div class="inspector-loading">Loading tree…</div>`;

  const messageEntries = tree.entries.filter(isMessageEntry);
  const visibleEntries = showTechnical ? tree.entries : messageEntries;
  const hiddenTechnicalCount = tree.entries.length - messageEntries.length;

  const loadOlder = async () => {
    if (loadingOlder) return;
    setLoadingOlder(true);
    try {
      await controller.loadMoreTree(sessionID);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setLoadingOlder(false);
    }
  };

  const previewEntry = async (entryID) => {
    if (previewingID || navigating) return;
    setPreviewingID(entryID);
    try {
      await controller.treePreview(sessionID, entryID);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setPreviewingID(null);
    }
  };

  const navigateToPreview = async () => {
    if (!preview || preview.current || navigating) return;
    setNavigating(true);
    try {
      await controller.navigateTree(sessionID, preview.targetID, summary || null);
      setSummary("");
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setNavigating(false);
    }
  };

  return html`<div class="inspector-stack tree-inspector">
    <div class="tree-toolbar">
      <div>
        <div class="tree-toolbar__title">Conversation tree</div>
        <div class="tree-toolbar__count" title=${`Tree revision ${tree.revision}`}>
          ${showTechnical
            ? `${tree.entries.length} loaded nodes`
            : `${messageEntries.length} messages${hiddenTechnicalCount ? ` · ${hiddenTechnicalCount} technical hidden` : ""}`}
        </div>
      </div>
      <div class="tree-toolbar__actions">
        ${hiddenTechnicalCount || showTechnical ? html`
          <button class="tree-view-toggle" disabled=${navigating} onClick=${() => setShowTechnical((value) => !value)}>
            ${showTechnical ? "Messages only" : "Show all nodes"}
          </button>
        ` : null}
        ${tree.cursor?.next ? html`
          <button class="secondary-action tree-load-more" disabled=${loadingOlder || navigating} onClick=${loadOlder}>
            ${loadingOlder ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
            <span>${loadingOlder ? "Loading" : "Older"}</span>
          </button>
        ` : null}
      </div>
    </div>

    <div class="tree-list" role="list" aria-label="Conversation tree nodes">
      ${visibleEntries.map((entry) => {
        const status = entry.current ? "Current" : entry.active ? "Active" : "Abandoned";
        const previewing = previewingID === entry.id;
        const selected = preview?.targetID === entry.id;
        const kind = !showTechnical && entry.kind === "assistant_tool_calls" ? "assistant" : entry.kind;
        return html`<div
          key=${entry.id}
          role="listitem"
          class=${`tree-entry ${entry.current ? "is-current" : ""} ${entry.active ? "is-active" : "is-abandoned"} ${selected ? "is-selected" : ""}`}
        >
          <div class="tree-entry__track" aria-hidden="true"><span class="tree-state"></span></div>
          <div class="tree-entry__content">
            <div class="tree-entry__head">
              <div class="tree-entry__identity">
                <span class="tree-kind">${kind}</span>
                ${entry.label ? html`<span class="tree-label">${entry.label}</span>` : null}
              </div>
              <span class="tree-entry__status">${status}</span>
            </div>
            ${entry.preview ? html`<div class="tree-preview-text">${entry.preview}</div>` : null}
            <div class="tree-entry__actions">
              ${entry.current
                ? html`<span class="tree-action-unavailable">Current node</span>`
                : html`<button disabled=${Boolean(previewingID) || navigating} onClick=${() => previewEntry(entry.id)}>
                    ${previewing ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
                    <span>${previewing ? "Previewing" : selected ? "Previewed" : "Preview"}</span>
                  </button>`}
              <button disabled=${navigating} onClick=${() => {
                const label = window.prompt("Node label", entry.label || "");
                if (label !== null) void controller.labelTreeEntry(sessionID, entry.id, label);
              }}>Label</button>
            </div>
          </div>
        </div>`;
      })}
    </div>

    ${!visibleEntries.length ? html`<div class="tree-empty">
      No user or assistant messages in this loaded window.${tree.cursor?.next ? " Load older nodes to continue." : ""}
    </div>` : null}

    ${preview ? html`<div class="navigation-preview">
      <div class="navigation-preview__head">
        <div>
          <div class="inspector-section-title">Navigation preview</div>
          <div class="navigation-preview__target">${preview.current ? "Already at this node" : "Review what changes before navigating"}</div>
        </div>
      </div>
      ${preview.editorText ? html`<div><span class="field-label">Restored editor text</span><pre>${preview.editorText}</pre></div>` : null}
      ${preview.abandoned?.length ? html`<div>
        <span class="field-label">Work that becomes abandoned${preview.abandonedTruncated ? `, showing ${preview.abandoned.length} of ${preview.abandonedTotal}` : ""}</span>
        <ul>${preview.abandoned.map((item) => html`<li><strong>${item.kind}</strong> ${item.preview || item.id}</li>`)}</ul>
      </div>` : html`<div class="muted">No active work is abandoned.</div>`}
      <label class="stacked-label">Branch summary, optional<textarea rows="3" value=${summary} disabled=${navigating} onInput=${(event) => setSummary(event.currentTarget.value)}></textarea></label>
      <button class="primary navigation-preview__action" disabled=${preview.current || navigating} onClick=${navigateToPreview}>
        ${navigating ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
        <span>${navigating ? "Navigating" : "Navigate here"}</span>
      </button>
    </div>` : null}
  </div>`;
}

function isMessageEntry(entry) {
  if (entry.kind === "user" || entry.kind === "assistant") return true;
  return entry.kind === "assistant_tool_calls" && Boolean(entry.preview);
}
