// @ts-check

import { api } from "../api/client.js";
import { store } from "./store.js";

/**
 * Owns permission/question snapshots and optimistic reply generations.
 */
export class HumanInputController {
  constructor({ lifecycle, actions }) {
    this.lifecycle = lifecycle;
    this.actions = actions;
    this.humanInputGeneration = new Map();
  }

  retire() {
    this.humanInputGeneration.clear();
  }

  async refreshHumanInput(sessionID, owns = () => true) {
    const generation = this.humanInputGeneration.get(sessionID) || 0;
    const lifecycleEpoch = this.lifecycle.epoch;
    const [permissions, questions] = await Promise.all([api.permissions(sessionID), api.questions(sessionID)]);
    if (this.lifecycle.epoch !== lifecycleEpoch || !owns()) return;
    if ((this.humanInputGeneration.get(sessionID) || 0) !== generation) return;
    this.installHumanInput(sessionID, permissions.data, questions.data);
  }

  installHumanInput(sessionID, permissions, questions) {
    store.setState((state) => ({
      ...state,
      attention: {
        ...state.attention,
        [sessionID]: { permissions: permissions.length, questions: questions.length },
      },
      sessionData: {
        ...state.sessionData,
        [sessionID]: { ...state.sessionData[sessionID], permissions, questions },
      },
    }));
  }

  async replyPermission(sessionID, requestID, reply, message = null) {
    const lifecycleEpoch = this.lifecycle.epoch;
    this.resolveHumanInputOptimistically(sessionID, "permissions", requestID);
    try {
      await api.replyPermission(sessionID, requestID, { reply, message: message || null });
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      await this.actions.refreshHumanInput(sessionID).catch(() => {});
      throw error;
    }
  }

  async replyQuestion(sessionID, requestID, answers) {
    const lifecycleEpoch = this.lifecycle.epoch;
    this.resolveHumanInputOptimistically(sessionID, "questions", requestID);
    try {
      await api.replyQuestion(sessionID, requestID, answers);
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      await this.actions.refreshHumanInput(sessionID).catch(() => {});
      throw error;
    }
  }

  async rejectQuestion(sessionID, requestID) {
    const lifecycleEpoch = this.lifecycle.epoch;
    this.resolveHumanInputOptimistically(sessionID, "questions", requestID);
    try {
      await api.rejectQuestion(sessionID, requestID);
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      await this.actions.refreshHumanInput(sessionID).catch(() => {});
      throw error;
    }
  }

  resolveHumanInputOptimistically(sessionID, field, requestID) {
    this.humanInputGeneration.set(
      sessionID,
      (this.humanInputGeneration.get(sessionID) || 0) + 1,
    );
    const data = store.getState().sessionData[sessionID] || {};
    const permissions = field === "permissions"
      ? (data.permissions || []).filter((item) => item.id !== requestID)
      : data.permissions || [];
    const questions = field === "questions"
      ? (data.questions || []).filter((item) => item.id !== requestID)
      : data.questions || [];
    this.installHumanInput(sessionID, permissions, questions);
  }
}
