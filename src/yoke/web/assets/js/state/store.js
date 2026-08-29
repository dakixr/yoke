// @ts-check

/** @typedef {(state: AppState) => unknown} Selector */

/**
 * @typedef {Object} AppState
 * @property {{status:string,current:boolean,error:string|null,serverInstanceID:string|null}} connection
 * @property {{required:boolean,token:string|null}} auth
 * @property {any|null} capabilities
 * @property {Object<string, any>} sessions
 * @property {string[]} sessionOrder
 * @property {string[]} archivedOrder
 * @property {number} archivedTotal
 * @property {string|null} sessionsCursor
 * @property {string|null} archivedCursor
 * @property {Object<string, any>} active
 * @property {Object<string, {permissions:number,questions:number}>} attention
 * @property {Object<string, any>} sessionData
 * @property {Object<string, any>} locations
 * @property {any[]} providers
 * @property {any[]} commands
 * @property {any[]} recentLocations
 * @property {{selectedSessionID:string|null,newSession:boolean,sidebarOpen:boolean,inspector:any|null,search:string,searching:boolean,searchResults:string[],commandPaletteOpen:boolean,notice:string|null,noticePending:boolean,doneUnreviewed:Object<string,boolean>}} ui
 * @property {Object<string, any>} drafts
 */

const initialState = () => ({
  connection: { status: "idle", current: false, error: null, serverInstanceID: null },
  auth: { required: false, token: null },
  capabilities: null,
  sessions: {},
  sessionOrder: [],
  archivedOrder: [],
  archivedTotal: 0,
  sessionsCursor: null,
  archivedCursor: null,
  active: {},
  attention: {},
  sessionData: {},
  locations: {},
  providers: [],
  commands: [],
  recentLocations: [],
  ui: {
    selectedSessionID: null,
    newSession: false,
    sidebarOpen: true,
    inspector: null,
    search: "",
    searching: false,
    searchResults: [],
    commandPaletteOpen: false,
    notice: null,
    noticePending: false,
    doneUnreviewed: {},
  },
  drafts: {},
});

let state = initialState();
const listeners = new Set();

export const store = {
  getState() {
    return state;
  },
  /** @param {(current: AppState) => AppState} updater */
  setState(updater) {
    const next = updater(state);
    if (next === state) return;
    state = next;
    for (const listener of listeners) listener();
  },
  /** @param {() => void} listener */
  subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  reset() {
    state = initialState();
    for (const listener of listeners) listener();
  },
};
