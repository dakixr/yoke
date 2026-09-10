import { html, useEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { copyText } from "../lib/clipboard.js";

export function CopyButton({ value, label = "Copy", title = "Copy content" }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return html`<button
    class="tool-field-copy"
    title=${title}
    aria-label=${title}
    onClick=${async () => {
      try {
        await copyText(value);
        setCopied(true);
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setCopied(false), 1200);
      } catch (error) {
        controller.notice(error?.message || String(error));
      }
    }}
  >${copied ? "Copied" : label}</button>`;
}
