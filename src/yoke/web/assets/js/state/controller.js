// @ts-check

import { InspectorStateController } from "../inspector/state/controller.js";
import { backInspector, loadInspectorFile, openInspector } from "../inspector/state/navigation.js";
import { BrowserLifecycle } from "./lifecycle.js";
import { store } from "./store.js";
import { WorkspaceController } from "./workspace/controller.js";
import { ConnectionController } from "./connection.js";
import { TranscriptController } from "./transcript.js";
import { InputController } from "./inputs.js";
import { HumanInputController } from "./human-input.js";
import { DraftController } from "./drafts.js";
import { ConfigurationController } from "./configuration.js";
import { CommandController } from "./commands.js";
import { SessionController } from "./sessions.js";
import { SessionNavigation } from "./navigation.js";

// Composition root and compatibility facade. State owners receive explicit collaborators.
export class AppController {
  constructor() {
    this.routeHandler = () => void this.applyRoute();
    // Forward callbacks through the public facade so callers can replace an action.
    const actions = (names) => Object.fromEntries(names.map((name) => [
      name, (...args) => this[name](...args),
    ]));
    this.lifecycle = new BrowserLifecycle({
      notice: (message) => this.notice(message),
      resetConnection: (options) => this.connection.reset(options),
      clearBuffers: () => this.connection.discardBufferedEvents(),
      retireOwners: () => {
        this.connection.retire();
        this.transcript.retire();
        this.sessions.retire();
        this.inputs.retire();
        this.humanInput.retire();
        this.configuration.retire();
        this.inspectorState.invalidateLifecycle();
      },
    });
    this.inspectorState = new InspectorStateController({
      lifecycleEpoch: () => this.lifecycleEpoch,
      refreshMessages: (sessionID) => this.refreshMessages(sessionID),
      notice: (message) => this.notice(message),
    });
    this.workspace = new WorkspaceController(this);
    this.inputs = new InputController({
      lifecycle: this.lifecycle,
      workspace: this.workspace,
      actions: actions(["notice", "refreshQueue", "schedule"]),
    });
    this.humanInput = new HumanInputController({
      lifecycle: this.lifecycle,
      actions: actions(["refreshHumanInput"]),
    });
    this.configuration = new ConfigurationController({
      lifecycle: this.lifecycle,
      inspectorState: this.inspectorState,
      workspace: this.workspace,
      actions: actions([
        "loadModels", "notice", "refreshMcp", "refreshSkills",
        "refreshTools", "schedule", "setSessionField", "updateDraft",
      ]),
    });
    this.sessions = new SessionController({
      lifecycle: this.lifecycle,
      workspace: this.workspace,
      actions: actions([
        "clearNotice", "loadSession", "pendingNotice", "refreshSessionLists",
        "resolveVisibleLocations", "selectSession",
      ]),
      inputs: this.inputs,
      configuration: this.configuration,
    });
    this.transcript = new TranscriptController({
      lifecycle: this.lifecycle,
      workspace: this.workspace,
      actions: actions(["refreshInputs", "refreshMessages", "schedule"]),
      inputs: this.inputs,
      humanInput: this.humanInput,
    });
    this.drafts = new DraftController({
      lifecycle: this.lifecycle,
      workspace: this.workspace,
      actions: actions([
        "clearNotice", "loadSession", "selectSession", "setSessionField",
        "submitPrompt",
      ]),
      inputs: this.inputs,
    });
    this.navigation = new SessionNavigation({
      inspectorState: this.inspectorState,
      actions: actions(["clearNotice", "createDraft", "loadSession", "selectSession"]),
    });
    this.commands = new CommandController({
      lifecycle: this.lifecycle,
      actions: actions([
        "activateSkill", "closeInspector", "compact", "createDraft",
        "createSessionFromDraft", "deleteDraft", "finishDraftSession", "forkSession",
        "openInspector", "patchSession", "refreshQueue", "regenerateTitle",
        "schedule",
      ]),
    });
    this.connection = new ConnectionController({
      lifecycle: this.lifecycle,
      workspace: this.workspace,
      actions: {
        ...actions([
          "applyRoute", "catchUpHistory", "listToolCalls", "loadSession",
          "loadToolCall", "notice", "persistDone", "refreshHumanInput",
          "refreshLiveToolCalls", "refreshMcp", "refreshMessages", "refreshProcessLocalState",
          "refreshProcessOutput", "refreshProcesses", "refreshQueue", "refreshSessionLists",
          "refreshSessionSummary", "refreshSkills", "refreshTools", "refreshTree",
          "resolveVisibleLocations", "resync", "retireLifecycle", "schedule",
          "scheduleLiveReconcile", "startSse", "throttle",
        ]),
        routeHandler: this.routeHandler,
      },
      inputs: this.inputs,
    });
    // Compatibility aliases for existing state diagnostics and regression fixtures.
    // Mutation ownership and retirement stay inside the corresponding module.
    this.refreshTimers = this.lifecycle.scheduler.timers;
    this.refreshTimerTasks = this.lifecycle.scheduler.tasks;
    this.treeServerRevisions = this.inspectorState.treeServerRevisions;
    this.treePendingLabels = this.inspectorState.treePendingLabels;
    this.messageRefreshGeneration = this.transcript.messageRefreshGeneration;
    this.liveToolRefreshGeneration = this.transcript.liveToolRefreshGeneration;
    this.queueMutationChains = this.inputs.queueMutationChains;
    this.queueServerRevisions = this.inputs.queueServerRevisions;
    this.queueMutationGeneration = this.inputs.queueMutationGeneration;
    this.queuePendingMutations = this.inputs.queuePendingMutations;
    this.humanInputGeneration = this.humanInput.humanInputGeneration;
    this.selectionGeneration = this.configuration.selectionGeneration;
    this.selectionMutationChains = this.configuration.selectionMutationChains;
    this.pendingSelections = this.configuration.pendingSelections;
    this.toolMutationChains = this.configuration.toolMutationChains;
    this.toolMutationGeneration = this.configuration.toolMutationGeneration;
    this.mcpMutationChains = this.configuration.mcpMutationChains;
    this.mcpMutationGeneration = this.configuration.mcpMutationGeneration;
    this.optimisticSessionGeneration = this.sessions.optimisticSessionGeneration;
    this.sessionMutationChains = this.sessions.sessionMutationChains;
    this.sessionPendingMutations = this.sessions.sessionPendingMutations;
  }

  get lifecycleEpoch() { return this.lifecycle.epoch; }

  get sse() { return this.connection.sse; }
  set sse(value) { this.connection.sse = value; }
  get bufferEvents() { return this.connection.bufferEvents; }
  set bufferEvents(value) { this.connection.bufferEvents = value; }
  get eventBuffer() { return this.connection.eventBuffer; }
  set eventBuffer(value) { this.connection.eventBuffer = value; }
  get bootstrapping() { return this.connection.bootstrapping; }
  set bootstrapping(value) { this.connection.bootstrapping = value; }
  get resyncing() { return this.connection.resyncing; }
  set resyncing(value) { this.connection.resyncing = value; }
  get bootstrapEpoch() { return this.connection.bootstrapEpoch; }
  set bootstrapEpoch(value) { this.connection.bootstrapEpoch = value; }
  get resyncEpoch() { return this.connection.resyncEpoch; }
  set resyncEpoch(value) { this.connection.resyncEpoch = value; }
  get broadResyncPending() { return this.connection.broadResyncPending; }
  set broadResyncPending(value) { this.connection.broadResyncPending = value; }
  get resyncPending() { return this.connection.resyncPending; }
  set resyncPending(value) { this.connection.resyncPending = value; }
  get pendingLiveEvents() { return this.connection.pendingLiveEvents; }
  set pendingLiveEvents(value) { this.connection.pendingLiveEvents = value; }
  get liveFrame() { return this.connection.liveFrame; }
  set liveFrame(value) { this.connection.liveFrame = value; }

  get searchGeneration() { return this.sessions.searchGeneration; }
  set searchGeneration(value) { this.sessions.searchGeneration = value; }

  start(...args) { return this.connection.start(...args); }
  stop(...args) { return this.connection.stop(...args); }
  retireLifecycle({ stopStream = false } = {}) {
    this.lifecycle.retire({ stopStream });
  }

  ownsLifecycle(lifecycleEpoch) { return this.lifecycle.owns(lifecycleEpoch); }

  consumeURLToken(...args) { return this.connection.consumeURLToken(...args); }
  authenticateAndBootstrap(...args) { return this.connection.authenticateAndBootstrap(...args); }
  setToken(...args) { return this.connection.setToken(...args); }
  bootstrap(...args) { return this.connection.bootstrap(...args); }
  startSse(...args) { return this.connection.startSse(...args); }
  onStreamState(...args) { return this.connection.onStreamState(...args); }
  receiveEvent(...args) { return this.connection.receiveEvent(...args); }
  applyEvents(...args) { return this.connection.applyEvents(...args); }
  drainBufferedEvents(...args) { return this.connection.drainBufferedEvents(...args); }
  reconcileAppliedEvent(...args) { return this.connection.reconcileAppliedEvent(...args); }
  requestResync(...args) { return this.connection.requestResync(...args); }
  startPendingBroadResync(...args) { return this.connection.startPendingBroadResync(...args); }
  scheduleEventRefresh(...args) { return this.connection.scheduleEventRefresh(...args); }
  schedule(key, delay, task) {
    this.lifecycle.scheduler.schedule(key, delay, task);
  }

  throttle(key, delay, task) {
    this.lifecycle.scheduler.throttle(key, delay, task);
  }

  scheduleLiveReconcile(...args) { return this.transcript.scheduleLiveReconcile(...args); }
  resync(...args) { return this.connection.resync(...args); }
  catchUpHistory(...args) { return this.transcript.catchUpHistory(...args); }
  refreshSessionLists(...args) { return this.sessions.refreshSessionLists(...args); }
  loadMoreSessions(...args) { return this.sessions.loadMoreSessions(...args); }
  countSessions(...args) { return this.sessions.countSessions(...args); }
  refreshSessionSummary(...args) { return this.sessions.refreshSessionSummary(...args); }
  resolveVisibleLocations(...args) { return this.sessions.resolveVisibleLocations(...args); }
  searchSessions(...args) { return this.sessions.searchSessions(...args); }
  applyRoute(...args) { return this.navigation.applyRoute(...args); }
  selectSession(...args) { return this.navigation.selectSession(...args); }
  markSessionUnread(...args) { return this.navigation.markSessionUnread(...args); }
  loadSession(...args) { return this.transcript.loadSession(...args); }
  loadOlderMessages(...args) { return this.transcript.loadOlderMessages(...args); }
  refreshMessages(...args) { return this.transcript.refreshMessages(...args); }
  refreshLiveToolCalls(...args) { return this.transcript.refreshLiveToolCalls(...args); }
  refreshInputs(...args) { return this.inputs.refreshInputs(...args); }
  resumeQueue(...args) { return this.inputs.resumeQueue(...args); }
  refreshQueue(...args) { return this.inputs.refreshQueue(...args); }
  refreshHumanInput(...args) { return this.humanInput.refreshHumanInput(...args); }
  refreshProcessLocalState(...args) { return this.connection.refreshProcessLocalState(...args); }
  async refreshTree(sessionID) { return this.inspectorState.refreshTree(sessionID); }

  async loadMoreTree(sessionID) { return this.inspectorState.loadMoreTree(sessionID); }

  refreshProcesses(...args) { return this.inspectorState.processes.refreshList(...args); }

  async refreshTools(sessionID) { return this.workspace.refreshTools(sessionID); }

  async refreshSkills(sessionID) { return this.workspace.refreshSkills(sessionID); }

  async refreshMcp(sessionID) { return this.workspace.refreshMcp(sessionID); }

  async loadModels(directory, provider = null, search = null) { return this.workspace.loadModels(directory, provider, search); }

  async loadProviders(directory = null) { return this.workspace.loadProviders(directory); }

  setSessionField(sessionID, key, value) {
    store.setState((state) => ({
      ...state,
      sessionData: { ...state.sessionData, [sessionID]: { ...state.sessionData[sessionID], [key]: value } },
    }));
  }

  installQueue(...args) { return this.inputs.installQueue(...args); }
  installAuthoritativeQueue(...args) { return this.inputs.installAuthoritativeQueue(...args); }
  installHumanInput(...args) { return this.humanInput.installHumanInput(...args); }
  createDraft(...args) { return this.drafts.createDraft(...args); }
  updateDraft(...args) { return this.drafts.updateDraft(...args); }
  deleteDraft(...args) { return this.drafts.deleteDraft(...args); }
  submitDraft(...args) { return this.drafts.submitDraft(...args); }
  createSessionFromDraft(...args) { return this.drafts.createSessionFromDraft(...args); }
  finishDraftSession(...args) { return this.drafts.finishDraftSession(...args); }
  slashSkillCompletions(...args) { return this.commands.slashSkillCompletions(...args); }
  slashMcpCompletions(...args) { return this.commands.slashMcpCompletions(...args); }
  runSlashCommand(...args) { return this.commands.runSlashCommand(...args); }
  submitPrompt(...args) { return this.inputs.submitPrompt(...args); }
  interrupt(...args) { return this.inputs.interrupt(...args); }
  interruptSelectedSession(...args) { return this.inputs.interruptSelectedSession(...args); }
  compact(...args) { return this.inputs.compact(...args); }
  upload(...args) { return this.inputs.upload(...args); }
  patchQueue(...args) { return this.inputs.patchQueue(...args); }
  async patchSession(...args) { return this.sessions.patchSession(...args); }
  regenerateTitle(...args) { return this.sessions.regenerateTitle(...args); }
  forkSession(...args) { return this.sessions.forkSession(...args); }
  replyPermission(...args) { return this.humanInput.replyPermission(...args); }
  replyQuestion(...args) { return this.humanInput.replyQuestion(...args); }
  rejectQuestion(...args) { return this.humanInput.rejectQuestion(...args); }
  resolveHumanInputOptimistically(...args) { return this.humanInput.resolveHumanInputOptimistically(...args); }
  toggleTool(...args) { return this.configuration.toggleTool(...args); }
  activateSkill(...args) { return this.configuration.activateSkill(...args); }
  patchMcp(...args) { return this.configuration.patchMcp(...args); }
  setSelection(...args) { return this.configuration.setSelection(...args); }
  cycleReasoningEffort(...args) { return this.configuration.cycleReasoningEffort(...args); }
  focusQueueEditor(...args) { return this.commands.focusQueueEditor(...args); }
  focusModelSelector(...args) { return this.commands.focusModelSelector(...args); }
  showShortcutHelp(...args) { return this.commands.showShortcutHelp(...args); }
  async openInspector(mode, payload = {}) {
    return openInspector(this, mode, payload);
  }

  backInspector() { return backInspector(this); }

  loadMoreToolCalls(sessionID) { return this.inspectorState.loadMoreToolCalls(sessionID); }

  showLatestToolCalls(sessionID) { return this.inspectorState.showLatestToolCalls(sessionID); }

  revealTreeHead(sessionID) { return this.inspectorState.revealTreeHead(sessionID); }

  closeInspector() {
    this.inspectorState.close();
  }

  async loadToolCall(sessionID, callID) { return this.inspectorState.loadToolCall(sessionID, callID); }

  async selectToolCall(sessionID, callID) { return this.inspectorState.selectToolCall(sessionID, callID); }

  async loadProcess(processID) { return this.inspectorState.loadProcess(processID); }

  async refreshProcessOutput(processID) { return this.inspectorState.refreshProcessOutput(processID); }

  findProcessDetail(processID) { return this.inspectorState.findProcessDetail(processID); }

  async refreshProcess(processID) { return this.inspectorState.refreshProcess(processID); }

  async loadFile(sessionID, path, selectionVersion = null) {
    return loadInspectorFile(this, sessionID, path, selectionVersion);
  }

  async treePreview(sessionID, targetID) { return this.inspectorState.treePreview(sessionID, targetID); }

  clearTreePreview(sessionID) {
    this.inspectorState.clearTreePreview(sessionID);
  }

  async navigateTree(sessionID, targetID, branchSummary = null) { return this.inspectorState.navigateTree(sessionID, targetID, branchSummary); }

  clearEditorHandoff(sessionID) {
    this.setSessionField(sessionID, "editorHandoff", null);
  }

  async labelTreeEntry(sessionID, entryID, label) { return this.inspectorState.labelTreeEntry(sessionID, entryID, label); }

  async listToolCalls(sessionID) { return this.inspectorState.listToolCalls(sessionID); }

  signalProcess(...args) { return this.inspectorState.processes.signalProcess(...args); }

  sendProcessInput(...args) { return this.inspectorState.processes.sendProcessInput(...args); }

  switchSession(...args) { return this.navigation.switchSession(...args); }
  toggleSidebar(...args) { return this.commands.toggleSidebar(...args); }
  togglePalette(...args) { return this.commands.togglePalette(...args); }
  runPaletteCommand(...args) { return this.commands.runPaletteCommand(...args); }
  escape(...args) { return this.commands.escape(...args); }
  notice(...args) { return this.commands.notice(...args); }
  pendingNotice(...args) { return this.commands.pendingNotice(...args); }
  clearNotice(...args) { return this.commands.clearNotice(...args); }
  fail(...args) { return this.connection.fail(...args); }
  persistDone(...args) { return this.navigation.persistDone(...args); }
}

export const controller = new AppController();
