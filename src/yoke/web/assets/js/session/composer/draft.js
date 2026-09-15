import { useEffect, useState } from "../../../vendor/htm-preact.js";
import { getSessionComposerDraft, subscribeSessionComposerDrafts } from "../../state/session-composer-drafts.js";

export function mergeRecoveredDraft(current, submittedText, submittedAttachments) {
  const currentText = current.text || "";
  const currentAttachments = current.attachments || [];
  if (!currentText.length && !currentAttachments.length) {
    return { text: submittedText, attachments: submittedAttachments };
  }
  const text = submittedText && currentText
    ? `${submittedText}\n\n${currentText}`
    : submittedText || currentText;
  const seen = new Set();
  const attachments = [...submittedAttachments, ...currentAttachments].filter((attachment) => {
    const key = attachment.uri || attachment.id || `${attachment.name || ""}:${attachment.size || ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return { text, attachments };
}

export function useSessionComposerDraft(sessionID) {
  const [, rerender] = useState(0);
  useEffect(() => subscribeSessionComposerDrafts(
    () => rerender((value) => value + 1),
  ), []);
  return getSessionComposerDraft(sessionID);
}

export function resizeComposerInput(input) {
  if (!input) return;
  input.style.height = "auto";
  const styles = window.getComputedStyle(input);
  const minHeight = Number.parseFloat(styles.minHeight) || 0;
  const parsedMaxHeight = Number.parseFloat(styles.maxHeight);
  const maxHeight = Number.isFinite(parsedMaxHeight) ? parsedMaxHeight : input.scrollHeight;
  input.style.height = `${Math.ceil(Math.max(minHeight, Math.min(input.scrollHeight, maxHeight)))}px`;
  input.style.overflowY = input.scrollHeight > maxHeight + 1 ? "auto" : "hidden";
}
