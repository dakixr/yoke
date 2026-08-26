import { marked } from "../../vendor/marked.esm.js";
import DOMPurify from "../../vendor/purify.es.mjs";

marked.setOptions({ gfm: true, breaks: false });

export function markdownHTML(text) {
  const dirty = marked.parse(text || "");
  return DOMPurify.sanitize(dirty, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "iframe", "object", "embed", "form"],
    FORBID_ATTR: ["style", "onerror", "onload"],
  });
}
