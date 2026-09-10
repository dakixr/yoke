import { html } from "../../../vendor/htm-preact.js";
import { isPlainObject, valueToText } from "../tool-logic.js";

export function ToolValue({ value, wrap = true, root = true }) {
  if (isPlainObject(value)) return html`<${ObjectValue} value=${value} wrap=${wrap} root=${root} />`;
  if (Array.isArray(value)) return html`<${ArrayValue} value=${value} wrap=${wrap} />`;
  if (typeof value === "string" && (value.includes("\n") || value.length > 110)) {
    return html`<pre class=${`tool-value__block ${wrap ? "is-wrapped" : ""}`}>${value}</pre>`;
  }
  return html`<code class=${`tool-value__scalar tool-value__scalar--${valueType(value)}`}>${scalarText(value)}</code>`;
}

function ObjectValue({ value, wrap, root }) {
  const entries = Object.entries(value);
  if (!entries.length) return html`<code class="tool-value__scalar tool-value__scalar--structure">{}</code>`;
  return html`<div class=${`tool-value__object ${root ? "is-root" : ""}`}>
    ${entries.map(([key, entry]) => html`<${FieldValue} key=${key} name=${key} value=${entry} wrap=${wrap} />`)}
  </div>`;
}

function FieldValue({ name, value, wrap }) {
  const block = isPlainObject(value) || Array.isArray(value) || (typeof value === "string" && (value.includes("\n") || value.length > 110));
  if (!block) {
    return html`<div class="tool-value__row"><span class="tool-value__key">${name}</span><${ToolValue} value=${value} wrap=${wrap} root=${false} /></div>`;
  }
  return html`<div class="tool-value__field">
    <div class="tool-value__field-head"><span class="tool-value__key">${name}</span><span>${describeValue(value)}</span></div>
    <div class="tool-value__nested"><${ToolValue} value=${value} wrap=${wrap} root=${false} /></div>
  </div>`;
}

function ArrayValue({ value, wrap }) {
  if (!value.length) return html`<code class="tool-value__scalar tool-value__scalar--structure">[]</code>`;
  if (value.length <= 6 && value.every((item) => !isPlainObject(item) && !Array.isArray(item) && !(typeof item === "string" && item.includes("\n")))) {
    return html`<code class="tool-value__array-inline">${valueToText(value)}</code>`;
  }
  return html`<div class="tool-value__array">
    ${value.map((item, index) => html`<div key=${index} class="tool-value__array-item"><span class="tool-value__index">${index}</span><${ToolValue} value=${item} wrap=${wrap} root=${false} /></div>`)}
  </div>`;
}

function valueType(value) {
  if (value === null) return "null";
  return typeof value;
}

function scalarText(value) {
  if (value === undefined) return "undefined";
  return typeof value === "string" ? JSON.stringify(value) : String(value);
}

function describeValue(value) {
  if (typeof value === "string") return `${value.length} chars`;
  if (Array.isArray(value)) return `${value.length} items`;
  if (isPlainObject(value)) return `${Object.keys(value).length} fields`;
  return "";
}
