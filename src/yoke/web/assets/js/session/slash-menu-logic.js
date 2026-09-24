export function slashMenuScrollDelta({ viewportTop, viewportBottom, itemTop, itemBottom }) {
  if (itemTop < viewportTop) return itemTop - viewportTop;
  if (itemBottom > viewportBottom) return itemBottom - viewportBottom;
  return 0;
}

export function skillMentionCompletionContext(text, cursorPosition = null) {
  const value = String(text || "");
  const cursor = Math.max(0, Math.min(value.length, cursorPosition ?? value.length));
  const beforeCursor = value.slice(0, cursor);
  const match = beforeCursor.match(/\$([A-Za-z0-9_:-]*)$/);
  if (!match) return null;
  const replaceStart = cursor - match[0].length;
  const preceding = replaceStart > 0 ? value[replaceStart - 1] : "";
  if (preceding && /[A-Za-z0-9_$\\]/.test(preceding)) return null;
  let replaceEnd = cursor;
  while (replaceEnd < value.length && /[A-Za-z0-9_:-]/.test(value[replaceEnd])) replaceEnd += 1;
  const token = value.slice(replaceStart + 1, replaceEnd);
  if (!skillNamePrefix(token)) return null;
  return {
    kind: "skillMention",
    token: match[1],
    replaceStart,
    replaceEnd,
  };
}

export function captureCompletionKeyWhileLoading(key, contextKind) {
  return key !== "Enter" || contextKind !== "skillMention";
}

function skillNamePrefix(token) {
  if (/^\d+$/.test(token)) return false;
  return /^(?:[a-z0-9]+(?:-[a-z0-9]*)*)?$/.test(token);
}
