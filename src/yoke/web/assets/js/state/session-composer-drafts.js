// @ts-check

import { readSessionComposerDrafts, writeSessionComposerDrafts } from "./local-state.js";

const EMPTY_DRAFT = Object.freeze({ text: "", attachments: [], updatedAt: "" });
let drafts = readSessionComposerDrafts();
const listeners = new Set();

export function getSessionComposerDraft(sessionID) {
  return drafts[sessionID] || EMPTY_DRAFT;
}

export function subscribeSessionComposerDrafts(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function updateSessionComposerDraft(sessionID, patch) {
  const current = getSessionComposerDraft(sessionID);
  const resolvedPatch = typeof patch === "function" ? patch(current) : patch;
  const draft = {
    ...current,
    ...resolvedPatch,
    text: resolvedPatch?.text ?? current.text ?? "",
    attachments: resolvedPatch?.attachments ?? current.attachments ?? [],
    updatedAt: new Date().toISOString(),
  };
  const next = { ...drafts };
  if (!draft.text.length && !draft.attachments.length) delete next[sessionID];
  else next[sessionID] = draft;
  install(next);
}

export function clearSessionComposerDraft(sessionID) {
  if (!(sessionID in drafts)) return;
  const next = { ...drafts };
  delete next[sessionID];
  install(next);
}

function install(next) {
  drafts = next;
  writeSessionComposerDrafts(drafts);
  for (const listener of listeners) listener();
}
