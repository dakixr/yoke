import { html } from "../../../vendor/htm-preact.js";
import { isPlainObject, valueToText } from "../tool-logic.js";
import { callArgumentState } from "./data.js";

const INLINE_LIMIT = 88;

export function CallSignature({ call, wrap = true }) {
  const name = call?.toolName || "tool";
  const { shown, raw } = callArgumentState(call);
  const entries = shown ? Object.entries(shown) : [];
  return html`<div class=${`tool-signature ${wrap ? "is-wrapped" : ""}`}>
    <div class="tool-signature__open"><span class="tool-signature__name">${name}</span><span>(</span></div>
    ${entries.length ? html`<div class="tool-signature__args">
      ${entries.map(([key, value]) => html`<${SignatureArgument} key=${key} name=${key} value=${value} wrap=${wrap} />`)}
    </div>` : raw?.trim() ? html`<pre class="tool-signature__raw">${raw}</pre>` : null}
    <div class="tool-signature__close">)</div>
  </div>`;
}

function SignatureArgument({ name, value, wrap }) {
  if (isInlineValue(value)) {
    return html`<div class="tool-signature__arg"><span class="tool-signature__key">${name}</span><span class="tool-signature__eq"> = </span><code>${inlineValue(value)}</code><span>,</span></div>`;
  }
  return html`<div class="tool-signature__arg tool-signature__arg--block">
    <div><span class="tool-signature__key">${name}</span><span class="tool-signature__eq"> =</span></div>
    <pre class=${wrap ? "is-wrapped" : ""}>${blockValue(value)}</pre>
  </div>`;
}

function isInlineValue(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number") return true;
  if (typeof value === "string") return !value.includes("\n") && JSON.stringify(value).length <= INLINE_LIMIT;
  if (Array.isArray(value)) return value.length === 0 || (value.length <= 4 && value.every(isShortScalar));
  if (isPlainObject(value)) return Object.keys(value).length === 0;
  return false;
}

function isShortScalar(value) {
  return value === null || typeof value === "boolean" || typeof value === "number" || (typeof value === "string" && !value.includes("\n") && value.length <= 28);
}

function inlineValue(value) {
  if (value === undefined) return "undefined";
  return typeof value === "string" ? JSON.stringify(value) : JSON.stringify(value);
}

function blockValue(value) {
  if (typeof value === "string") return value;
  return valueToText(value);
}
