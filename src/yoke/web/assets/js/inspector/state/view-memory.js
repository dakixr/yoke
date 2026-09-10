import { store } from "../../state/store.js";

export const EMPTY_PREFERENCES = Object.freeze({});

/** View state lives with its session, not in localStorage or a mounted tab. */
export function inspectorPreferences(state, sessionID, mode) {
  return state.sessionData[sessionID]?.inspectorPreferences?.[mode] || EMPTY_PREFERENCES;
}

export function patchInspectorPreferences(sessionID, mode, patch, defaults = {}) {
  if (!sessionID) return;
  store.setState((state) => {
    const data = state.sessionData[sessionID] || {};
    const previous = inspectorPreferences(state, sessionID, mode);
    const update = typeof patch === "function" ? patch({ ...defaults, ...previous }) : patch;
    if (!update || Object.entries(update).every(([key, value]) => Object.is(previous[key], value))) return state;
    return {
      ...state,
      sessionData: {
        ...state.sessionData,
        [sessionID]: {
          ...data,
          inspectorPreferences: {
            ...data.inspectorPreferences,
            [mode]: { ...previous, ...update },
          },
        },
      },
    };
  });
}
