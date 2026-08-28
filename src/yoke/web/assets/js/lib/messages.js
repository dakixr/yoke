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
