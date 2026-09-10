import { defaultTreeEntries, displayTreeEntries } from "../tree-graph.js";
import { projectedMessageText } from "../../lib/messages.js";

export function treeView(tree, showTechnical = false) {
  const entries = (tree?.entries || []).map((entry) => ({
    ...entry,
    current: entry.id === tree.leafID,
  }));
  const messages = defaultTreeEntries(entries);
  const messageIDs = new Set(messages.map((entry) => entry.id));
  const visible = showTechnical ? entries : entries.filter((entry) => messageIDs.has(entry.id) || entry.current);
  return {
    entries,
    rows: displayTreeEntries(entries, visible).map((entry) => ({
      ...entry,
      technicalAnchor: !showTechnical && entry.current && !messageIDs.has(entry.id),
    })),
    messageCount: messages.length,
    hiddenCount: entries.length - visible.length,
    anchorCount: showTechnical ? 0 : visible.length - messages.length,
    latestID: entries.at(-1)?.id || null,
  };
}

// Find, rather than filter: forks and the active lane do not jump as you type.
export function treeMatches(entries, query) {
  const terms = String(query || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  return (entries || []).filter((entry) => {
    const text = `${entry.label || ""} ${entry.preview || ""} ${entry.id} ${kindLabel(entry.kind)}`.toLocaleLowerCase();
    return terms.every((term) => text.includes(term));
  }).map((entry) => entry.id);
}

export function nextTreeMatch(matches, currentID, direction = 1) {
  if (!matches.length) return null;
  const index = matches.indexOf(currentID);
  if (index < 0) return direction < 0 ? matches.at(-1) : matches[0];
  return matches[(index + direction + matches.length) % matches.length];
}

export function kindLabel(kind) {
  const normalized = String(kind || "node").replace(/^assistant_tool_calls$/, "assistant").replace(/_/g, " ");
  if (normalized === "user") return "You";
  if (normalized === "assistant") return "Assistant";
  return normalized;
}

export function entryText(entry) {
  return entry?.preview || entry?.label || `${kindLabel(entry?.kind)} ${entry?.id || ""}`;
}

export function destinationText(entry, messages, preview) {
  if (entry && preview && preview.targetID === entry.id && preview.editorText != null) {
    return { text: preview.editorText, partial: false };
  }
  const message = (messages || []).find((item) => item.id === entry?.id);
  const text = projectedMessageText(message);
  return text ? { text, partial: false } : { text: entryText(entry), partial: true };
}

export function formatTreeTime(value, { details = false } = {}) {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, {
    ...(details ? { year: "numeric", second: "2-digit", timeZoneName: "short" } : {}),
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

export function navigationConsequence(entry, preview) {
  if (entry?.current || preview?.current) return "This is the current HEAD. Your next prompt already continues here.";
  if (!preview) return "Checking what will leave active context…";
  if (entry?.active) {
    return "Continuing from this earlier point removes its later active descendants from active context. Nothing is deleted; you can return to them in the tree.";
  }
  if (preview.abandonedTotal) {
    return `${preview.abandonedTotal} active ${preview.abandonedTotal === 1 ? "node leaves" : "nodes leave"} active context when you switch to this branch. Nothing is deleted; you can return to the old path in the tree.`;
  }
  return "Your next prompt will use the path to this destination. Nothing is deleted.";
}
