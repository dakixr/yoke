import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { copyText } from "../lib/clipboard.js";
import {
  countLines,
  describeBlockValue,
  formatCompactValue,
  isClampedText,
  isCompactValue,
  isPlainObject,
  scalarListText,
  toFields,
  valueKind,
  valueToText,
} from "./tool-logic.js";

const MAX_NESTED_DEPTH = 2;
const MAX_LIST_CHIPS = 12;

/**
 * A titled card holding one structured payload (arguments, result, …).
 */
export function ToolFieldCard({ title, note, fields, text, wrap, copyValue }) {
  if (!fields?.length && !text) return null;
  return html`<section class="tool-field-card">
    <div class="tool-detail-section-head">
      <span>${title}</span>
      <span class="tool-field-card__tail">
        ${note ? html`<span>${note}</span>` : null}
        ${copyValue ? html`<${CopyButton} value=${copyValue} />` : null}
      </span>
    </div>
    <div class="tool-field-card__body">
      ${fields?.length
        ? html`<${ToolFieldGroup} fields=${fields} wrap=${wrap} depth=${0} />`
        : html`<${ToolCodeBlock} text=${text} wrap=${wrap} />`}
    </div>
  </section>`;
}

/**
 * Scalars collapse into a chip row; anything long or structured gets its own
 * labelled block, so a payload reads as fields instead of a JSON dump.
 */
export function ToolFieldGroup({ fields, wrap, depth }) {
  const compact = fields.filter((field) => isCompactValue(field.value));
  const blocks = fields.filter((field) => !isCompactValue(field.value));
  return html`<div class="tool-fields">
    ${compact.length ? html`<div class="tool-field-chips">
      ${compact.map((field) => html`<span key=${field.key} class="tool-field-chip">
        <b>${field.key}</b>
        <span class=${`tool-field-value tool-field-value--${valueKind(field.value)}`}>${formatCompactValue(field.value)}</span>
        ${field.tag ? html`<em title=${field.tagTitle || null}>${field.tag}</em>` : null}
      </span>`)}
    </div>` : null}
    ${blocks.map((field) => html`<${ToolFieldBlock} key=${field.key} field=${field} wrap=${wrap} depth=${depth} />`)}
  </div>`;
}

function ToolFieldBlock({ field, wrap, depth }) {
  const { key, value } = field;
  const nested = depth < MAX_NESTED_DEPTH && isPlainObject(value);
  const scalarList = Array.isArray(value) && value.every(isCompactValue);
  const list = scalarList && value.length <= MAX_LIST_CHIPS;
  return html`<div class="tool-field-block">
    <div class="tool-field-block__head">
      <span class="tool-field-block__key">${key}</span>
      ${field.tag ? html`<em title=${field.tagTitle || null}>${field.tag}</em>` : null}
      <span class="tool-field-block__meta">${describeBlockValue(value)}</span>
    </div>
    ${nested ? html`<div class="tool-field-block__nested">
      <${ToolFieldGroup} fields=${toFields(value)} wrap=${wrap} depth=${depth + 1} />
    </div>` : list ? html`<div class="tool-field-chips tool-field-chips--list">
      ${value.map((item, index) => html`<span key=${index} class=${`tool-field-value tool-field-value--${valueKind(item)}`}>${formatCompactValue(item)}</span>`)}
    </div>` : html`<${ToolCodeBlock} text=${scalarList ? scalarListText(value) : valueToText(value)} wrap=${wrap} />`}
  </div>`;
}

/**
 * Monospace payload that clamps tall content behind an expander.
 */
export function ToolCodeBlock({ text, wrap }) {
  const [expanded, setExpanded] = useState(false);
  const clampable = isClampedText(text);
  return html`<div class=${`tool-code ${clampable && !expanded ? "is-clamped" : ""}`}>
    <pre class=${wrap ? "is-wrapped" : ""}>${text}</pre>
    ${clampable ? html`<button class="tool-code__expand" onClick=${() => setExpanded((value) => !value)}>
      ${expanded ? "Show less" : `Show all ${countLines(text)} lines`}
    </button>` : null}
  </div>`;
}

function CopyButton({ value }) {
  const [copied, setCopied] = useState(false);
  return html`<button
    class="tool-field-copy"
    title="Copy as JSON"
    onClick=${async () => {
      try {
        await copyText(value);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      } catch (error) {
        controller.notice(error?.message || String(error));
      }
    }}
  >${copied ? "Copied" : "Copy"}</button>`;
}
