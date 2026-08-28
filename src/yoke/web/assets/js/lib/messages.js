export function projectedMessageText(message) {
  return (message?.content || [])
    .filter((part) => part.type === "text")
    .map((part) => part.text || "")
    .join("\n");
}

export function effectiveAssistantPhase(message) {
  if (message?.phase) return message.phase;
  if (message?.toolCalls?.length && projectedMessageText(message)) return "commentary";
  return null;
}

export function assistantMetadataMessageIDs(messages) {
  const ids = new Set();
  let lastTextAssistantID = null;
  for (const message of messages || []) {
    if (message?.type === "user") {
      if (lastTextAssistantID) ids.add(lastTextAssistantID);
      lastTextAssistantID = null;
      continue;
    }
    if (message?.type !== "assistant") continue;
    if (!projectedMessageText(message).trim()) continue;
    lastTextAssistantID = message.id || null;
  }
  if (lastTextAssistantID) ids.add(lastTextAssistantID);
  return ids;
}

export function compactToolBatchMessageIDs(messages) {
  const ids = new Set();
  const items = messages || [];
  for (let index = 0; index < items.length; index += 1) {
    const message = items[index];
    if (!isToolOnlyAssistant(message)) continue;
    for (let nextIndex = index + 1; nextIndex < items.length; nextIndex += 1) {
      const next = items[nextIndex];
      if (next?.type === "user") break;
      if (next?.type !== "assistant") continue;
      if (isToolOnlyAssistant(next) && message.id) ids.add(message.id);
      break;
    }
  }
  return ids;
}

function isToolOnlyAssistant(message) {
  return Boolean(
    message?.type === "assistant"
      && message.toolCalls?.length
      && !projectedMessageText(message).trim()
      && !(message.content || []).some((part) => part.type === "image"),
  );
}
