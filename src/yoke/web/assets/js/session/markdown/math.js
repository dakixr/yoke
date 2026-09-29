import katex from "../../../vendor/katex/katex.mjs";

const delimiters = [
  { left: "$$", right: "$$", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "\\[", right: "\\]", display: true },
  { left: "$", right: "$", display: false },
];

function findEnd(source, delimiter, inlineRules) {
  const singleDollar = delimiter.left === "$";
  let braces = 0;
  for (let index = delimiter.left.length; index < source.length; index += 1) {
    const char = source[index];
    if (singleDollar && (char === "\n" || char === "\r")) return -1;
    if (braces === 0 && source.startsWith(delimiter.right, index)) {
      // Stop at the first candidate. Skipping a price's dollar could consume
      // prose all the way to the next formula in "$25 and $30, then $x$".
      if (singleDollar && (
        /\s/.test(source[index - 1]) || /[\d$]/.test(source[index + 1] || "")
      )) return -1;
      return index;
    }
    if (singleDollar && braces === 0) {
      if (char === "`" || source.startsWith("\\(", index) || source.startsWith("\\[", index)) return -1;
      // A stray price/opener must not reach into a code element or link URL.
      if (char === "<" && inlineRules.tag.test(source.slice(index))) return -1;
      if ((char === "[" || char === "!") && inlineRules.link.test(source.slice(index))) return -1;
    }
    if (char === "\\") {
      if (singleDollar && /[\r\n]/.test(source[index + 1] || "")) return -1;
      index += 1;
    } else if (char === "{") braces += 1;
    else if (char === "}") braces = Math.max(0, braces - 1);
  }
  return -1;
}

function mathToken(source, inlineRules) {
  const delimiter = delimiters.find(({ left }) => source.startsWith(left));
  if (!delimiter) return;
  const literal = { type: "yokeMath", raw: delimiter.left, text: null };
  if (delimiter.left === "$" && (!source[1] || /\s/.test(source[1]))) return literal;
  const end = findEnd(source, delimiter, inlineRules);
  if (end < 0) return literal;
  const raw = source.slice(0, end + delimiter.right.length);
  const text = source.slice(delimiter.left.length, end);
  if (!text.trim()) return { ...literal, raw };
  return { type: "yokeMath", raw, text, display: delimiter.display };
}

function escapeHTML(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function renderSource(token) {
  if (token.text === null) return escapeHTML(token.raw);
  return `<span data-yoke-math="${escapeHTML(token.text)}" data-math-display="${token.display}">${escapeHTML(token.source || token.raw)}</span>`;
}

// Tokenize before Markdown unescapes text, instead of reparsing rendered text.
// Marked owns code spans, fences, link destinations, and escaped delimiters.
export const mathExtension = {
  extensions: [
    {
      name: "yokeMath",
      level: "inline",
      start(source) { return source.search(/\$|\\(?:\(|\[)/); },
      tokenizer(source) {
        if (!this.lexer.state.inRawBlock) return mathToken(source, this.lexer.tokenizer.rules.inline);
      },
      renderer: renderSource,
    },
    {
      name: "yokeMathBlock",
      level: "block",
      start(source) { return source.search(/\n {0,3}(?:\$\$|\\\[)/); },
      tokenizer(source) {
        const opening = /^( {0,3})(?:\$\$|\\\[)/.exec(source);
        if (!opening) return;
        const token = mathToken(source.slice(opening[1].length), this.lexer.tokenizer.rules.inline);
        if (!token || token.text === null) return;
        const length = opening[1].length + token.raw.length;
        const ending = /^[ \t]*(?:\n|$)/.exec(source.slice(length));
        if (!ending) return;
        return { ...token, type: "yokeMathBlock", source: token.raw, raw: source.slice(0, length + ending[0].length) };
      },
      renderer(token) { return `<p>${renderSource(token)}</p>\n`; },
    },
  ],
};

export function renderMarkdownMath(element) {
  if (!element) return;
  for (const node of element.querySelectorAll("span[data-yoke-math]")) {
    if (node.closest("pre, code, script, style, textarea, .katex")) continue;
    const container = document.createElement("span");
    try {
      katex.render(node.getAttribute("data-yoke-math"), container, {
        displayMode: node.getAttribute("data-math-display") === "true",
        throwOnError: true,
        trust: false,
      });
      node.replaceWith(...container.childNodes);
    } catch {
      node.replaceWith(document.createTextNode(node.textContent));
    }
  }
}
