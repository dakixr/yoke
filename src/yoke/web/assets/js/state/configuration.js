// @ts-check

import { api } from "../api/client.js";
import { currentRoute } from "../router/router.js";
import { isBusy } from "./session-machine.js";
import { store } from "./store.js";

const SUMMARY_REFRESH_MS = 250;

/**
 * Owns serialized model, tool and MCP mutations and pending model selections.
 */
export class ConfigurationController {
  constructor({ lifecycle, inspectorState, workspace, actions }) {
    this.lifecycle = lifecycle;
    this.inspectorState = inspectorState;
    this.workspace = workspace;
    this.actions = actions;
    this.selectionGeneration = new Map();
    this.selectionMutationChains = new Map();
    this.pendingSelections = new Map();
    this.toolMutationChains = new Map();
    this.toolMutationGeneration = new Map();
    this.mcpMutationChains = new Map();
    this.mcpMutationGeneration = new Map();
  }

  retire() {
    this.selectionGeneration.clear();
    this.selectionMutationChains.clear();
    this.pendingSelections.clear();
    this.toolMutationChains.clear();
    this.toolMutationGeneration.clear();
    this.mcpMutationChains.clear();
    this.mcpMutationGeneration.clear();
  }

  projectSelection(summary) {
    const selection = this.pendingSelections.get(summary.id);
    return selection ? { ...summary, selection } : summary;
  }

  projectPendingSelections(state) {
    let next = state;
    for (const [sessionID, selection] of this.pendingSelections) {
      const session = next.sessions[sessionID];
      if (!session) continue;
      next = { ...next, sessions: { ...next.sessions, [sessionID]: { ...session, selection } } };
    }
    return next;
  }

  async toggleTool(sessionID, toolName, enabled) {
    this.inspectorState.invalidateRequest(sessionID, "config:tools");
    const current = store.getState().sessionData[sessionID]?.tools || [];
    const key = `${sessionID}:${toolName}`;
    const generation = (this.toolMutationGeneration.get(key) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.toolMutationGeneration.set(key, generation);
    if (current.length) {
      this.actions.setSessionField(
        sessionID,
        "tools",
        current.map((tool) => tool.name === toolName ? { ...tool, enabled } : tool),
      );
    }
    const prior = this.toolMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      try {
        const response = await api.patchTools(
          sessionID,
          enabled ? { enabled: [toolName], disabled: [] } : { enabled: [], disabled: [toolName] },
        );
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (
          this.toolMutationGeneration.get(key) === generation &&
          Array.isArray(response.data?.enabled)
        ) {
          const effective = new Set(response.data.enabled);
          const tools = store.getState().sessionData[sessionID]?.tools || [];
          this.actions.setSessionField(
            sessionID,
            "tools",
            tools.map((tool) => tool.name === toolName
              ? { ...tool, enabled: effective.has(toolName) }
              : tool),
          );
        }
        return response.data;
      } catch (error) {
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (this.toolMutationGeneration.get(key) === generation) {
          await this.actions.refreshTools(sessionID).catch(() => {});
          if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.toolMutationChains.get(sessionID) === chained) {
        this.toolMutationChains.delete(sessionID);
      }
    });
    this.toolMutationChains.set(sessionID, chained);
    return chained;
  }

  async activateSkill(sessionID, skillName, prompt = null) {
    this.workspace.require(sessionID);
    const lifecycleEpoch = this.lifecycle.epoch;
    this.inspectorState.invalidateRequest(sessionID, "config:skills");
    const skills = store.getState().sessionData[sessionID]?.skills;
    const candidate = skills?.available?.find((skill) => skill.name === skillName) || null;
    if (skills && candidate && !(skills.active || []).some((skill) => skill.name === skillName)) {
      this.actions.setSessionField(sessionID, "skills", {
        ...skills,
        active: [...(skills.active || []), { ...candidate, active: true }],
      });
    }
    try {
      const response = await api.activateSkill(sessionID, skillName, prompt);
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      if (skills && response.data?.activated) {
        const current = store.getState().sessionData[sessionID]?.skills || skills;
        this.actions.setSessionField(sessionID, "skills", {
          ...current,
          active: [
            ...(current.active || []).filter((skill) => skill.name !== skillName),
            response.data.activated,
          ],
        });
      }
      return response.data;
    } catch (error) {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      await this.actions.refreshSkills(sessionID).catch(() => {});
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      throw error;
    }
  }

  async patchMcp(sessionID, serverName, patch) {
    this.inspectorState.invalidateRequest(sessionID, "config:mcp");
    const current = store.getState().sessionData[sessionID]?.mcp || [];
    const key = `${sessionID}:${serverName}`;
    const generation = (this.mcpMutationGeneration.get(key) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.mcpMutationGeneration.set(key, generation);
    if (current.length) {
      this.actions.setSessionField(
        sessionID,
        "mcp",
        current.map((server) => server.name === serverName
          ? {
              ...server,
              ...(Object.prototype.hasOwnProperty.call(patch, "enabled") ? { enabled: patch.enabled } : {}),
              ...(Array.isArray(patch.enabledTools) ? { enabledTools: patch.enabledTools } : {}),
              ...(Array.isArray(patch.disabledTools) ? { disabledTools: patch.disabledTools } : {}),
            }
          : server),
      );
    }
    const prior = this.mcpMutationChains.get(key) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      try {
        const response = await api.patchMcp(sessionID, serverName, patch);
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (this.mcpMutationGeneration.get(key) === generation) {
          this.actions.schedule(`mcp:${sessionID}`, SUMMARY_REFRESH_MS, () => this.actions.refreshMcp(sessionID));
        }
        return response.data;
      } catch (error) {
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (this.mcpMutationGeneration.get(key) === generation) {
          await this.actions.refreshMcp(sessionID).catch(() => {});
          if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.mcpMutationChains.get(key) === chained) {
        this.mcpMutationChains.delete(key);
      }
    });
    this.mcpMutationChains.set(key, chained);
    return chained;
  }

  async setSelection(sessionID, provider, model, reasoningEffort) {
    this.workspace.require(sessionID);
    const previous = store.getState().sessions[sessionID]?.selection || null;
    const previousContextUsage = store.getState().sessionData[sessionID]?.contextUsage || null;
    const generation = (this.selectionGeneration.get(sessionID) || 0) + 1;
    const lifecycleEpoch = this.lifecycle.epoch;
    this.selectionGeneration.set(sessionID, generation);
    const desired = {
      provider,
      model,
      reasoningEffort: reasoningEffort || null,
    };
    this.pendingSelections.set(sessionID, desired);
    store.setState((state) => {
      const session = state.sessions[sessionID];
      if (!session) return state;
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [sessionID]: {
            ...session,
            selection: desired,
            contextUsage: null,
          },
        },
        sessionData: {
          ...state.sessionData,
          [sessionID]: {
            ...state.sessionData[sessionID],
            contextUsage: null,
          },
        },
      };
    });

    const prior = this.selectionMutationChains.get(sessionID) || Promise.resolve();
    const task = prior.catch(() => {}).then(async () => {
      if (!this.lifecycle.owns(lifecycleEpoch)) return null;
      try {
        const response = await api.selectModel(sessionID, desired);
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (this.selectionGeneration.get(sessionID) === generation) {
          this.pendingSelections.delete(sessionID);
          const effective = response.data?.effective;
          if (effective) {
            store.setState((state) => {
              const session = state.sessions[sessionID];
              if (!session) return state;
              return {
                ...state,
                sessions: {
                  ...state.sessions,
                  [sessionID]: { ...session, selection: effective, contextUsage: null },
                },
              };
            });
          }
        }
        return response.data;
      } catch (error) {
        if (!this.lifecycle.owns(lifecycleEpoch)) return null;
        if (this.selectionGeneration.get(sessionID) === generation) {
          this.pendingSelections.delete(sessionID);
          if (previous) {
            store.setState((state) => {
              const session = state.sessions[sessionID];
              if (!session) return state;
              return {
                ...state,
                sessions: {
                  ...state.sessions,
                  [sessionID]: { ...session, selection: previous, contextUsage: previousContextUsage },
                },
                sessionData: {
                  ...state.sessionData,
                  [sessionID]: {
                    ...state.sessionData[sessionID],
                    contextUsage: previousContextUsage,
                  },
                },
              };
            });
          }
        }
        throw error;
      }
    });
    const chained = task.finally(() => {
      if (this.selectionMutationChains.get(sessionID) === chained) {
        this.selectionMutationChains.delete(sessionID);
      }
    });
    this.selectionMutationChains.set(sessionID, chained);
    return chained;
  }

  async cycleReasoningEffort() {
    const state = store.getState();
    const route = currentRoute();
    let directory = "";
    let provider = "";
    let model = "";
    let currentEffort = "";
    let sessionID = null;
    let draftID = null;
    if (route.name === "session") {
      sessionID = route.sessionID;
      const session = state.sessions[sessionID];
      if (!session) return false;
      const runtime = state.active[sessionID];
      if (isBusy(runtime)) {
        this.actions.notice("Thinking effort can be changed when the current turn settles.");
        return false;
      }
      directory = session.location?.directory || "";
      provider = session.selection?.provider || "";
      model = session.selection?.model || "";
      currentEffort = session.selection?.reasoningEffort || "";
    } else if (route.name === "new") {
      draftID = route.draftID;
      const draft = state.drafts[draftID];
      if (!draft) return false;
      directory = draft.location || "";
      provider = draft.provider || "";
      model = draft.model || "";
      currentEffort = draft.reasoningEffort || "";
    } else {
      return false;
    }
    if (!directory || !provider || !model) return false;
    const modelKey = `${directory}:${provider}:`;
    const models = state.models[modelKey]?.length
      ? state.models[modelKey]
      : await this.actions.loadModels(directory, provider);
    const info = models.find((item) => item.id === model);
    const values = (info?.reasoningEfforts || []).filter(Boolean);
    if (!values.length) return false;
    const normalized = String(currentEffort || "high").toLowerCase();
    let index = values.findIndex((value) => String(value).toLowerCase() === normalized);
    if (index < 0) index = values.findIndex((value) => String(value).toLowerCase() === "high");
    const next = values[(index + 1 + values.length) % values.length];
    if (draftID) this.actions.updateDraft(draftID, { reasoningEffort: next });
    if (sessionID) await this.setSelection(sessionID, provider, model, next);
    this.actions.notice(`Thinking effort: ${next}`);
    return true;
  }
}
