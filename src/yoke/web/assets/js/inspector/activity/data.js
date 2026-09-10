import { isPlainObject, parseJSONObject, valueToText } from "../tool-logic.js";

export function callArgumentState(call) {
  const sent = parseJSONObject(call?.arguments?.raw);
  const executed = isPlainObject(call?.arguments?.executed) ? call.arguments.executed : null;
  return {
    sent,
    executed,
    shown: sent || executed,
    raw: typeof call?.arguments?.raw === "string" ? call.arguments.raw : null,
  };
}

export function compactCallSignature(call, maxLength = 180) {
  const name = call?.toolName || "tool";
  const { shown, raw } = callArgumentState(call);
  let body;
  if (shown) {
    body = Object.entries(shown).map(([key, value]) => `${key}=${compactValue(value)}`).join(", ");
  } else if (raw?.trim()) {
    body = raw.replace(/\s+/g, " ").trim();
  } else {
    body = "";
  }
  const rendered = `${name}(${body})`;
  if (rendered.length <= maxLength) return rendered;
  return `${rendered.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}

export function capturedOutput(detail) {
  return (detail?.outputChunks || []).map((chunk) => chunk.text || "").join("");
}

export function projectedResultValue(projection) {
  if (typeof projection !== "string") return null;
  try {
    return { kind: "json", value: JSON.parse(projection), text: projection };
  } catch {
    return { kind: "text", value: null, text: projection };
  }
}

function compactValue(value) {
  if (typeof value === "string") {
    const one = value.replace(/\s+/g, " ").trim();
    const clipped = one.length > 44 ? `${one.slice(0, 43)}…` : one;
    return JSON.stringify(clipped);
  }
  const rendered = valueToText(value).replace(/\s+/g, " ");
  return rendered.length > 52 ? `${rendered.slice(0, 51)}…` : rendered;
}
