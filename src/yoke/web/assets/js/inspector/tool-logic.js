export function sortToolCallsChronologically(calls) {
  const ordered = [...(calls || [])];
  if (ordered.every((call) => Number.isFinite(call.sequence))) {
    return ordered.sort((left, right) => left.sequence - right.sequence);
  }
  if (ordered.some((call) => Number.isFinite(call.sequence))) return ordered;
  if (ordered.some((call) => !Number.isFinite(Date.parse(call.time?.started)))) return ordered;
  return ordered.sort((left, right) => Date.parse(left.time.started) - Date.parse(right.time.started));
}

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

export function valueToText(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
