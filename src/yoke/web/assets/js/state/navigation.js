// @ts-check

import { currentRoute, draftPath, navigate, sessionPath } from "../router/router.js";
import { readReviewed, writeDone, writeReviewed } from "./local-state.js";
import { adjacentVisualSessionID } from "./session-order.js";
import { store } from "./store.js";

/**
 * Owns route selection and persisted reviewed/unread state.
 */
export class SessionNavigation {
  constructor({ inspectorState, actions }) {
    this.inspectorState = inspectorState;
    this.actions = actions;
  }

  async applyRoute() {
    const route = currentRoute();
    if (route.name === "session") {
      this.actions.selectSession(route.sessionID, { navigate: false });
      await this.actions.loadSession(route.sessionID);
      return;
    }
    if (route.name === "new") {
      const id = route.draftID || this.actions.createDraft({ navigate: false });
      if (!route.draftID) navigate(draftPath(id), { replace: true });
      store.setState((state) => ({
        ...state,
        ui: { ...state.ui, selectedSessionID: null, newSession: true, sidebarOpen: window.innerWidth > 760 },
      }));
      return;
    }
    if (route.name === "home") {
      const first = store.getState().sessionOrder[0];
      if (first) {
        history.replaceState({}, "", sessionPath(first));
        this.actions.selectSession(first, { navigate: false });
        await this.actions.loadSession(first);
      } else {
        const id = this.actions.createDraft({ navigate: false });
        history.replaceState({}, "", draftPath(id));
        store.setState((state) => ({
          ...state,
          ui: {
            ...state.ui,
            selectedSessionID: null,
            newSession: true,
            sidebarOpen: window.innerWidth > 760,
          },
        }));
      }
    }
  }

  selectSession(sessionID, { navigate: shouldNavigate = true } = {}) {
    this.actions.clearNotice();
    if (store.getState().ui.selectedSessionID !== sessionID) {
      this.inspectorState.invalidateSelection();
    }
    const done = { ...store.getState().ui.doneUnreviewed, [sessionID]: false };
    const reviewed = readReviewed();
    reviewed[sessionID] = new Date().toISOString();
    writeReviewed(reviewed);
    writeDone(done);
    store.setState((state) => ({
      ...state,
      ui: {
        ...state.ui,
        selectedSessionID: sessionID,
        newSession: false,
        sidebarOpen: window.innerWidth > 760 ? state.ui.sidebarOpen : false,
        doneUnreviewed: done,
      },
    }));
    if (shouldNavigate) navigate(sessionPath(sessionID));
  }

  markSessionUnread(sessionID) {
    const done = { ...store.getState().ui.doneUnreviewed, [sessionID]: true };
    writeDone(done);
    store.setState((state) => ({
      ...state,
      ui: { ...state.ui, doneUnreviewed: done },
    }));
  }

  switchSession(delta) {
    const state = store.getState();
    const next = adjacentVisualSessionID(
      state.sessionOrder,
      state.sessions,
      state.ui.selectedSessionID,
      delta,
    );
    if (!next) return;
    this.actions.selectSession(next);
  }

  persistDone() {
    writeDone(store.getState().ui.doneUnreviewed);
  }
}
