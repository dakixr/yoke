import katex from "../../vendor/katex/katex.mjs";
import { marked } from "../../vendor/marked.esm.js";
import DOMPurify from "../../vendor/purify.es.mjs";

marked.setOptions({ gfm: true, breaks: false });

const mathDelimiters = [
  { left: "$$", right: "$$", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "\\[", right: "\\]", display: true },
];
const mathTokenOpen = "\uE000";
const mathTokenClose = "\uE001";

function findMathEnd(text, delimiter, startIndex) {
  let index = startIndex;
  let braceLevel = 0;
  while (index < text.length) {
    if (braceLevel <= 0 && text.startsWith(delimiter, index)) return index;
    const character = text[index];
    if (character === "\\") {
      index += 2;
      continue;
    }
    if (character === "{") braceLevel += 1;
    else if (character === "}") braceLevel -= 1;
    index += 1;
  }
  return -1;
}

function nextMathStart(text, startIndex) {
  let match = null;
  for (const delimiter of mathDelimiters) {
    const index = text.indexOf(delimiter.left, startIndex);
    if (index < 0 || (match && match.index <= index)) continue;
    match = { index, delimiter };
  }
  return match;
}

function splitMathText(text) {
  const segments = [];
  let cursor = 0;
  let searchIndex = 0;
  let foundMath = false;
  while (searchIndex < text.length) {
    const start = nextMathStart(text, searchIndex);
    if (!start) break;
    const contentStart = start.index + start.delimiter.left.length;
    const end = findMathEnd(text, start.delimiter.right, contentStart);
    if (end < 0) {
      searchIndex = contentStart;
      continue;
    }
    if (start.index > cursor) {
      segments.push({ type: "text", value: text.slice(cursor, start.index) });
    }
    const rawEnd = end + start.delimiter.right.length;
    segments.push({
      type: "math",
      value: text.slice(contentStart, end),
      raw: text.slice(start.index, rawEnd),
      display: start.delimiter.display,
    });
    foundMath = true;
    cursor = rawEnd;
    searchIndex = rawEnd;
  }
  if (!foundMath) return null;
  if (cursor < text.length) {
    segments.push({ type: "text", value: text.slice(cursor) });
  }
  return segments;
}

function mathToken(index) {
  return `${mathTokenOpen}${index.toString(36)}${mathTokenClose}`;
}

function escapeHTML(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function maskMath(text) {
  const segments = splitMathText(text);
  if (!segments) return { text, sources: [] };
  const sources = [];
  return {
    text: segments.map((segment) => {
      if (segment.type === "text") return segment.value;
      const token = mathToken(sources.length);
      sources.push(segment.raw);
      return token;
    }).join(""),
    sources,
  };
}

function restoreMath(html, sources) {
  return sources.reduce((value, source, index) => {
    const escaped = escapeHTML(source);
    return value.replaceAll(mathToken(index), () => escaped);
  }, html);
}

export function markdownHTML(text) {
  const masked = maskMath(text || "");
  const dirty = marked.parse(masked.text);
  const clean = DOMPurify.sanitize(dirty, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "iframe", "object", "embed", "form"],
    FORBID_ATTR: ["style", "onerror", "onload"],
  });
  return restoreMath(clean, masked.sources);
}

export function renderMarkdownMath(element) {
  if (!element) return;
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.parentElement?.closest("pre, code, script, style, textarea")) continue;
    textNodes.push(node);
  }
  for (const node of textNodes) {
    const segments = splitMathText(node.textContent || "");
    if (!segments) continue;
    const fragment = document.createDocumentFragment();
    for (const segment of segments) {
      if (segment.type === "text") {
        fragment.append(document.createTextNode(segment.value));
        continue;
      }
      const container = document.createElement("span");
      try {
        katex.render(segment.value, container, {
          displayMode: segment.display,
          throwOnError: true,
          trust: false,
        });
        while (container.firstChild) fragment.append(container.firstChild);
      } catch {
        fragment.append(document.createTextNode(segment.raw));
      }
    }
    node.replaceWith(fragment);
  }
}
