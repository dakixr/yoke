// @ts-check

import { api } from "../api/client.js";
import { randomUUID } from "../lib/id.js";
import { draftPath, navigate } from "../router/router.js";
import { readDrafts, writeDrafts } from "./local-state.js";
import { installSessionSummary } from "./optimistic-projections.js";
import { store } from "./store.js";

/**
 * Owns new-session drafts and their persistence, creation and submission workflow.
 */
export class DraftController {
  constructor({ lifecycle, workspace, actions, inputs }) {
    this.lifecycle = lifecycle;
    this.workspace = workspace;
    this.actions = actions;
    this.inputs = inputs;
  }

  createDraft({ navigate: shouldNavigate = true, location = null, selection = null } = {}) {
    this.actions.clearNotice();
    const id = `draft_${randomUUID()}`;
    const state = store.getState();
    const recent = location || state.recentLocations[0]?.directory || "";
    const defaultProvider = state.providers.find((item) => item.ready && item.currentModel) || null;
    const draft = {
      id,
      text: "",
      location: recent,
      provider: selection?.provider || defaultProvider?.id || "",
      model: selection?.model || defaultProvider?.currentModel || "",
      reasoningEffort: selection?.reasoningEffort || defaultProvider?.currentReasoningEffort || "",
      attachments: [],
      updatedAt: new Date().toISOString(),
    };
    store.setState((state) => ({ ...state, drafts: { ...state.drafts, [id]: draft } }));
    if (shouldNavigate) navigate(draftPath(id));
    return id;
  }

  updateDraft(id, patch) {
    const current = store.getState().drafts[id] || { id, attachments: [] };
    const draft = { ...current, ...patch, id, updatedAt: new Date().toISOString() };
    const drafts = { ...store.getState().drafts, [id]: draft };
    store.setState((state) => ({ ...state, drafts }));
    const persistedDrafts = { ...readDrafts() };
    if ((draft.text || "").trim() || draft.attachments?.length) persistedDrafts[id] = draft;
    else delete persistedDrafts[id];
    writeDrafts(persistedDrafts);
  }

  deleteDraft(id) {
    const drafts = { ...store.getState().drafts };
    delete drafts[id];
    const persistedDrafts = { ...readDrafts() };
    delete persistedDrafts[id];
    writeDrafts(persistedDrafts);
    store.setState((state) => ({ ...state, drafts }));
  }

  async submitDraft(id, { delivery = "steer", background = false } = {}) {
    const lifecycleEpoch = this.lifecycle.epoch;
    const draft = store.getState().drafts[id];
    if (!draft) throw new Error("Draft was not found.");
    if (!(draft.text || "").trim() && !draft.attachments?.length) throw new Error("Write a prompt or attach an image first.");
    const sessionID = await this.createSessionFromDraft(id);
    if (!this.lifecycle.owns(lifecycleEpoch) || !sessionID) return;
    if (!background) this.actions.selectSession(sessionID);
    try {
      await this.actions.submitPrompt(sessionID, {
        text: draft.text || "",
        attachments: draft.attachments || [],
        delivery,
      });
      if (!this.lifecycle.owns(lifecycleEpoch)) return;
      this.deleteDraft(id);
      if (background) {
        this.createDraft({
          location: draft.location || null,
          selection: {
            provider: draft.provider || "",
            model: draft.model || "",
            reasoningEffort: draft.reasoningEffort || "",
          },
        });
      }
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return;
      if (!background && draft.text) this.actions.setSessionField(sessionID, "editorHandoff", draft.text);
      throw error;
    }
  }

  async createSessionFromDraft(id, { titleText = null } = {}) {
    const draft = store.getState().drafts[id];
    if (!draft) throw new Error("Draft was not found.");
    const location = draft.location || store.getState().recentLocations[0]?.directory;
    if (!location) throw new Error("Choose a working location first.");
    const sessionID = `web_${randomUUID()}`;
    const lifecycleEpoch = this.lifecycle.epoch;
    const selection = draft.provider && draft.model ? {
      provider: draft.provider,
      model: draft.model,
      reasoningEffort: draft.reasoningEffort || null,
    } : undefined;
    const response = await api.createSession({
      id: sessionID,
      location: { directory: location },
      title: titleText === null ? null : provisionalSessionTitle(titleText),
      selection,
    });
    if (!this.lifecycle.owns(lifecycleEpoch)) return null;
    store.setState((state) => {
      const next = installSessionSummary(state, response.data, { moveToFront: true });
      const revision = response.data.queue?.revision || 0;
      return {
        ...next,
        attention: {
          ...next.attention,
          [sessionID]: { permissions: 0, questions: 0 },
        },
        sessionData: {
          ...next.sessionData,
          [sessionID]: {
            ...next.sessionData[sessionID],
            loaded: true,
            loading: false,
            messageSnapshotLoaded: false,
            messages: next.sessionData[sessionID]?.messages || [],
            queue: next.sessionData[sessionID]?.queue || { revision, items: [] },
            permissions: next.sessionData[sessionID]?.permissions || [],
            questions: next.sessionData[sessionID]?.questions || [],
          },
        },
      };
    });
    this.inputs.rememberRevision(sessionID, response.data.queue?.revision || 0);
    // A project becomes "recent" the moment it holds a session, so the location
    // picker must learn about it without waiting for a page reload.
    void this.workspace.refreshLocations(location).catch(() => {});
    return sessionID;
  }

  async finishDraftSession(id, sessionID) {
    const lifecycleEpoch = this.lifecycle.epoch;
    this.deleteDraft(id);
    this.actions.selectSession(sessionID);
    await this.actions.loadSession(sessionID, { force: true });
    if (!this.lifecycle.owns(lifecycleEpoch)) return;
  }
}

function provisionalSessionTitle(text) {
  const title = String(text || "").trim().replace(/\s+/g, " ").slice(0, 72);
  return title || "New session";
}
