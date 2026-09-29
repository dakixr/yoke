import { Marked } from "../../vendor/marked.esm.js";
import DOMPurify from "../../vendor/purify.es.mjs";
import { mathExtension } from "./markdown/math.js";

export { renderMarkdownMath } from "./markdown/math.js";

const markdown = new Marked({ gfm: true, breaks: false }, mathExtension);

export function markdownHTML(text) {
  return DOMPurify.sanitize(markdown.parse(text || ""), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "iframe", "object", "embed", "form"],
    FORBID_ATTR: ["style", "onerror", "onload"],
  });
}
