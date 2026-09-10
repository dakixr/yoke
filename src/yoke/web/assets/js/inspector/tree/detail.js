import { html, useLayoutEffect, useRef, useState } from "../../../vendor/htm-preact.js";
import { controller } from "../../state/controller.js";
import { destinationText, entryText, formatTreeTime, kindLabel, navigationConsequence } from "./model.js";

export function TreeDetail({ sessionID, entry, parent, messages, state, ready, summary, setSummary, onContinue, onClear, onBack, onRetry }) {
  const preview = state.preview;
  const context = destinationText(entry, messages, preview);
  const bodyRef = useRef(null);
  useLayoutEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [entry?.id]);
  return html`<aside class="tree-detail" aria-label="Continue from a destination">
    <div class="tree-detail__heading">
      <button class="tree-detail__back" onClick=${onBack}>Back to tree</button>
      <h2>${entry ? "Continue from here" : "Choose a destination"}</h2>
      ${entry ? html`<button disabled=${state.moving} onClick=${onClear} aria-label="Clear destination">Clear</button>` : null}
    </div>
    <div class="tree-detail__body" ref=${bodyRef}>
      ${entry ? html`
        <div class="tree-detail__identity">
          <span>${kindLabel(entry.kind)}</span>
          ${entry.current ? html`<strong>Current HEAD</strong>` : null}
          <time datetime=${entry.createdAt} title=${entry.createdAt}>${formatTreeTime(entry.createdAt, { details: true })}</time>
        </div>
        <${TreeLabelEditor} key=${`${sessionID}:${entry.id}`} sessionID=${sessionID} entry=${entry} disabled=${state.moving} />
        ${parent ? html`<details class="tree-detail__previous"><summary>Preceding ${kindLabel(parent.kind).toLowerCase()}</summary><p>${entryText(parent)}</p></details>` : null}
        ${context.partial ? html`<span class="tree-detail__caption">Message preview</span>` : null}
        <p class="tree-detail__message">${context.text}</p>
        <p class="tree-detail__consequence" role="status">${state.error ? "Refresh the preview before continuing." : navigationConsequence(entry, preview)}</p>
        ${state.error ? html`<div class="tree-detail__error" role="alert"><p>${state.error}</p><button onClick=${onRetry}>Refresh preview</button></div>` : null}
        ${preview?.editorText != null ? html`<section class="tree-detail__editor"><h3>Prompt restored to composer</h3><p>You will continue before this user message, with its text ready to edit.</p></section>` : null}
        ${preview?.abandoned?.length ? html`<details class="tree-detail__leaving">
          <summary>Leaving active context · ${preview.abandonedTruncated ? `${preview.abandoned.length} of ${preview.abandonedTotal}` : preview.abandoned.length} ${preview.abandonedTotal === 1 ? "node" : "nodes"}</summary>
          <ul>${preview.abandoned.map((item) => html`<li key=${item.id}><span>${kindLabel(item.kind)}</span><p>${item.preview || item.id}</p></li>`)}</ul>
        </details>` : null}
        ${preview?.abandonedTotal ? html`<details key=${entry.id} class="tree-detail__handoff"><summary>Add a handoff note · optional</summary>
          <label>What should the new path remember?<textarea rows="4" value=${summary} disabled=${state.moving} onInput=${(event) => setSummary(event.currentTarget.value)} /></label>
        </details>` : null}
      ` : html`<p class="tree-detail__empty">Select a message in the tree. Review the destination here, then continue when you're ready. Selecting does not move HEAD.</p>`}
    </div>
    <div class="tree-detail__footer">
      <button class="tree-continue" disabled=${!ready} onKeyDown=${(event) => {
        if (event.repeat && (event.key === "Enter" || event.key === " ")) event.preventDefault();
      }} onClick=${onContinue}>${state.moving ? "Continuing…" : state.pending ? "Loading preview…" : "Continue from here"}</button>
      <span>${entry?.current ? "Already current" : "Nothing is deleted."}</span>
    </div>
  </aside>`;
}

function TreeLabelEditor({ sessionID, entry, disabled }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);
  const editButtonRef = useRef(null);
  useLayoutEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);
  const close = () => {
    setEditing(false);
    requestAnimationFrame(() => editButtonRef.current?.focus());
  };
  const save = async (event) => {
    event.preventDefault();
    if (saving || disabled) return;
    setSaving(true);
    setError(null);
    try {
      const result = await controller.labelTreeEntry(sessionID, entry.id, draft);
      if (result) close();
      else setError("Label was not saved. Review the current label and try again.");
    } catch (error) {
      setError(error?.message || String(error));
    } finally {
      setSaving(false);
    }
  };
  if (!editing) return html`<div class="tree-detail__label"><strong>${entry.label || ""}</strong><button ref=${editButtonRef} disabled=${disabled} onClick=${() => {
    setDraft(entry.label || "");
    setEditing(true);
  }}>${entry.label ? "Edit label" : "Add label"}</button></div>`;
  return html`<form class="tree-label-editor" onSubmit=${save} onKeyDown=${(event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!saving) close();
    }
  }}>
    <label>Node label<input ref=${inputRef} value=${draft} disabled=${saving || disabled} onInput=${(event) => setDraft(event.currentTarget.value)} /></label>
    <div><button type="submit" disabled=${saving || disabled}>${saving ? "Saving…" : "Save label"}</button><button type="button" disabled=${saving} onClick=${close}>Cancel</button></div>
    ${error ? html`<p role="alert">${error}</p>` : null}
  </form>`;
}
