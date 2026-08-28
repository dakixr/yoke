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
