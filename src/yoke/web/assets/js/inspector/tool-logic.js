export function sortToolCallsChronologically(calls) {
  return [...(calls || [])].sort((left, right) => {
    const leftTime = Date.parse(left.time?.started || "") || 0;
    const rightTime = Date.parse(right.time?.started || "") || 0;
    if (leftTime !== rightTime) return leftTime - rightTime;
    return String(left.id || "").localeCompare(String(right.id || ""));
  });
}

const COMPACT_STRING_LIMIT = 72;
const CLAMP_LINES = 16;
const CLAMP_CHARS = 1400;

export function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function parseJSONObject(text) {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  if (!trimmed.startsWith("{")) return null;
  try {
    const parsed = JSON.parse(trimmed);
    return isPlainObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Collapse the model-sent arguments and the normalized executed arguments into
 * one list of fields. Divergence between the two is kept as a per-field tag so
 * the detail view never needs a second, duplicated arguments panel.
 */
export function buildArgumentFields(argumentsInfo) {
  const sent = parseJSONObject(argumentsInfo?.raw);
  const executed = isPlainObject(argumentsInfo?.executed) ? argumentsInfo.executed : null;
  const base = executed || sent;
  if (!base) {
    const text = typeof argumentsInfo?.raw === "string" ? argumentsInfo.raw.trim() : "";
    return { fields: [], text };
  }
  const fields = Object.entries(base).map(([key, value]) => ({ key, value, ...compareOrigin(key, value, executed, sent) }));
  if (executed && sent) {
    for (const [key, value] of Object.entries(sent)) {
      if (key in executed) continue;
      fields.push({ key, value, tag: "dropped", tagTitle: "Sent by the model but not passed to the tool." });
    }
  }
  return { fields, text: "" };
}

function compareOrigin(key, value, executed, sent) {
  if (!executed || !sent) return {};
  if (!(key in sent)) return { tag: "default", tagTitle: "Filled in by the tool; the model did not send it." };
  const before = JSON.stringify(sent[key]);
  if (before === JSON.stringify(value)) return {};
  return { tag: "adjusted", tagTitle: `Model sent: ${before}` };
}

/**
 * Result fields, minus any payload already rendered by the output pane.
 */
export function buildResultFields(result, outputText) {
  if (!isPlainObject(result)) return { fields: [], text: typeof result === "string" ? result : "" };
  const shown = String(outputText || "").trim();
  const fields = Object.entries(result)
    .filter(([, value]) => !(shown && typeof value === "string" && value.trim() && shown.includes(value.trim())))
    .map(([key, value]) => ({ key, value }));
  return { fields, text: "" };
}

export function toFields(value) {
  return isPlainObject(value) ? Object.entries(value).map(([key, entry]) => ({ key, value: entry })) : [];
}

export function isCompactValue(value) {
  if (value === null || value === undefined) return true;
  if (typeof value === "boolean" || typeof value === "number") return true;
  if (typeof value === "string") return !value.includes("\n") && value.length <= COMPACT_STRING_LIMIT;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value).length === 0;
  return true;
}

export function valueKind(value) {
  if (value === null || value === undefined) return "empty";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return "number";
  if (typeof value === "string") return value === "" ? "empty" : "text";
  return "structure";
}

export function formatCompactValue(value) {
  if (value === null) return "null";
  if (value === undefined) return "—";
  if (typeof value === "string") return value === "" ? "empty" : value;
  if (Array.isArray(value)) return "[ ]";
  if (typeof value === "object") return "{ }";
  return String(value);
}

export function valueToText(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/** One scalar per line reads better than a JSON array of strings. */
export function scalarListText(value) {
  return value.map((item) => (typeof item === "string" ? item : formatCompactValue(item))).join("\n");
}

export function describeBlockValue(value) {
  if (typeof value === "string") {
    const lines = countLines(value);
    return `${value.length} chars${lines > 1 ? ` · ${lines} lines` : ""}`;
  }
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
  if (isPlainObject(value)) {
    const keys = Object.keys(value).length;
    return `${keys} field${keys === 1 ? "" : "s"}`;
  }
  return "";
}

export function countLines(text) {
  const value = String(text ?? "");
  if (!value) return 0;
  return value.split("\n").length;
}

export function isClampedText(text) {
  return countLines(text) > CLAMP_LINES || String(text ?? "").length > CLAMP_CHARS;
}
