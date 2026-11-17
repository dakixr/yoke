// @ts-check

import { api } from "../api/client.js";
import { store } from "./store.js";

/**
 * Owns slash/palette dispatch, focus actions and notice state in the UI store.
 */
export class CommandController {
  constructor({ lifecycle, actions }) {
    this.lifecycle = lifecycle;
    this.actions = actions;
  }

  async slashSkillCompletions(directory, search = "") {
    const response = await api.skills({ directory, search: search || null });
    return response.data || [];
  }

  async slashMcpCompletions(sessionID, directory, search = "") {
    const response = sessionID
      ? await api.sessionMcp(sessionID, false)
      : await api.mcp({ directory, includeTools: false });
    return (response.data || [])
      .filter((server) => !search || String(server.name || "").toLowerCase().includes(search.toLowerCase()))
      .map((server) => ({
        name: server.name,
        description: `${server.transport || "MCP"} · ${server.status || (server.enabled ? "enabled" : "disabled")}`,
      }));
  }

  async runSlashCommand(text, { sessionID = null, draftID = null, directory = "" } = {}) {
    const parsed = parseSlashCommand(text);
    if (!parsed) return { handled: false };
    const { name, args } = parsed;
    if (name === "shortcuts" || name === "?") {
      if (args) return slashUsage((message) => this.notice(message), name === "?" ? "?" : "/shortcuts");
      this.showShortcutHelp();
      return { handled: true };
    }
    if (name === "image") {
      if (args) {
        this.notice("Browser image attachments use the file picker, drag and drop, or paste. Use /image without a path.");
        return { handled: true, clear: false };
      }
      return { handled: true, action: "image" };
    }
    if (name === "new") {
      if (args) return slashUsage((message) => this.notice(message), "/new");
      if (draftID) this.actions.deleteDraft(draftID);
      this.actions.createDraft({ location: directory || null });
      return { handled: true };
    }
    if (name === "model") {
      if (args) return slashUsage((message) => this.notice(message), "/model");
      requestAnimationFrame(() => this.focusModelSelector());
      return { handled: true };
    }
    if (name === "skill") {
      const [skillName, prompt] = splitFirstArgument(args);
      if (!skillName) {
        this.notice("Usage: /skill <name> [prompt]");
        return { handled: true, clear: false };
      }
      if (sessionID) {
        await this.actions.activateSkill(sessionID, skillName, prompt || null);
        await this.actions.refreshQueue(sessionID);
        this.notice(`Activated skill: ${skillName}`);
        return { handled: true };
      }
      if (draftID) {
        const available = await this.slashSkillCompletions(directory, skillName);
        if (!available.some((skill) => skill.name === skillName)) {
          this.notice(`Unknown skill: ${skillName}`);
          return { handled: true, clear: false };
        }
        const createdID = await this.actions.createSessionFromDraft(draftID, {
          titleText: prompt || `Use ${skillName}`,
        });
        try {
          await this.actions.activateSkill(createdID, skillName, prompt || null);
          this.notice(`Activated skill: ${skillName}`);
        } finally {
          await this.actions.finishDraftSession(draftID, createdID);
        }
        return { handled: true };
      }
    }
    if (!sessionID) {
      this.notice(`/${name} is available after a session starts.`);
      return { handled: true, clear: false };
    }
    const session = store.getState().sessions[sessionID];
    if (name === "compact") {
      if (args) return slashUsage((message) => this.notice(message), "/compact");
      await this.actions.compact(sessionID);
      return { handled: true };
    }
    if (name === "pin") {
      if (args) return slashUsage((message) => this.notice(message), "/pin");
      await this.actions.patchSession(sessionID, { pinned: !session?.pinned });
      return { handled: true };
    }
    if (name === "info") {
      if (args) return slashUsage((message) => this.notice(message), "/info");
      await this.actions.openInspector("context");
      return { handled: true };
    }
    if (name === "fork") {
      if (args) return slashUsage((message) => this.notice(message), "/fork");
      await this.actions.forkSession(sessionID);
      return { handled: true };
    }
    if (name === "title") {
      if (!args) return slashUsage((message) => this.notice(message), "/title <new-title>");
      await this.actions.patchSession(sessionID, { title: args });
      return { handled: true };
    }
    if (name === "regenerate-title") {
      if (args) return slashUsage((message) => this.notice(message), "/regenerate-title");
      this.pendingNotice("Regenerating title…");
      const updated = await this.actions.regenerateTitle(sessionID);
      this.notice(`Title updated to “${updated.title}”.`);
      return { handled: true };
    }
    if (name === "tree") {
      if (args) return slashUsage((message) => this.notice(message), "/tree");
      await this.actions.openInspector("tree");
      return { handled: true };
    }
    if (name === "tools") {
      if (args) return slashUsage((message) => this.notice(message), "/tools");
      await this.actions.openInspector("tools");
      return { handled: true };
    }
    if (name === "mcp") {
      await this.actions.openInspector("mcp", args ? { serverName: args.split(/\s+/)[0] } : {});
      return { handled: true };
    }
    if (name === "queue") {
      if (args) return slashUsage((message) => this.notice(message), "/queue");
      this.focusQueueEditor();
      return { handled: true };
    }
    if (name === "ps") {
      if (args) return slashUsage((message) => this.notice(message), "/ps");
      await this.actions.openInspector("process");
      return { handled: true };
    }
    this.notice(`Unknown command: /${name}`);
    return { handled: true, clear: false };
  }

  focusQueueEditor() {
    const queue = document.querySelector(".queue-editor");
    if (!queue) {
      this.notice("No queued messages.");
      return false;
    }
    queue.scrollIntoView({ block: "nearest" });
    queue.querySelector("textarea, button, select")?.focus();
    return true;
  }

  focusModelSelector() {
    const model = document.querySelector(".model-picker__trigger");
    if (!model || model.disabled) {
      this.notice("Model selection is available when the session is idle.");
      return false;
    }
    model.focus();
    model.click();
    return true;
  }

  showShortcutHelp() {
    this.notice("⌘K / Ctrl+K commands · ⇧⌘O / ⇧Ctrl+O new session · ⌘B / Ctrl+B sessions · Alt+↑/↓ switch session · ⌘Enter / Ctrl+Enter background new session · Enter send/steer · Tab queue · ⇧Tab effort · Esc Esc stop · ⇧Enter/Ctrl+J/Esc Enter newline · Ctrl+U remove image. In Tree: arrows choose a destination, Enter continues after its preview loads, Escape clears the destination first. Tab stays inside the inspector.");
    return true;
  }

  toggleSidebar() {
    store.setState((state) => ({ ...state, ui: { ...state.ui, sidebarOpen: !state.ui.sidebarOpen } }));
  }

  togglePalette(open = !store.getState().ui.commandPaletteOpen) {
    if (open) this.clearNotice();
    store.setState((state) => ({ ...state, ui: { ...state.ui, commandPaletteOpen: open } }));
  }

  async runPaletteCommand(command) {
    const state = store.getState();
    const sessionID = state.ui.selectedSessionID;
    this.togglePalette(false);
    if (command.action === "session.create") return this.actions.createDraft();
    if (command.action === "command.list") return this.showShortcutHelp();
    if (!sessionID) return this.notice("Select a session first.");
    const session = state.sessions[sessionID];
    if (command.name === "pin") return this.actions.patchSession(sessionID, { pinned: !session?.pinned });
    if (command.name === "title") {
      const title = window.prompt("Session title", session?.title || "");
      if (title !== null) return this.actions.patchSession(sessionID, { title });
      return;
    }
    if (command.action === "session.title.regenerate") {
      this.pendingNotice("Regenerating title…");
      try {
        const updated = await this.actions.regenerateTitle(sessionID);
        return this.notice(`Title updated to “${updated.title}”.`);
      } catch (error) {
        return this.notice(error?.message || String(error));
      }
    }
    if (command.action === "session.compact") return this.actions.compact(sessionID);
    if (command.action === "session.get") return this.actions.openInspector("context");
    if (command.action === "session.fork") return this.actions.forkSession(sessionID);
    if (command.action === "session.tree") return this.actions.openInspector("tree");
    if (command.action === "session.tool") return this.actions.openInspector("tools");
    if (command.action === "session.mcp") return this.actions.openInspector("mcp");
    if (command.action === "process.list") return this.actions.openInspector("process");
    if (command.action === "agent.list") return this.actions.openInspector("agents");
    if (command.action === "session.skill") return this.actions.openInspector("skills");
    if (command.action === "session.queue") return this.focusQueueEditor();
    if (command.action === "session.selection") return this.focusModelSelector();
    if (command.action === "upload.create") return this.notice("Paste or drop an image into the composer, or use the Image button.");
  }

  escape() {
    const state = store.getState();
    if (state.ui.commandPaletteOpen) {
      this.togglePalette(false);
      return true;
    }
    if (state.ui.inspector) {
      this.actions.closeInspector();
      return true;
    }
    if (window.innerWidth <= 760 && state.ui.sidebarOpen) {
      this.toggleSidebar();
      return true;
    }
    return false;
  }

  notice(message) {
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: message, noticePending: false } }));
    this.actions.schedule("notice", 3200, () => {
      store.setState((state) => ({ ...state, ui: { ...state.ui, notice: null, noticePending: false } }));
    });
  }

  pendingNotice(message) {
    clearTimeout(this.lifecycle.scheduler.timers.get("notice"));
    this.lifecycle.scheduler.timers.delete("notice");
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: message, noticePending: true } }));
  }

  clearNotice() {
    clearTimeout(this.lifecycle.scheduler.timers.get("notice"));
    this.lifecycle.scheduler.timers.delete("notice");
    if (!store.getState().ui.notice) return;
    store.setState((state) => ({ ...state, ui: { ...state.ui, notice: null, noticePending: false } }));
  }
}

function parseSlashCommand(text) {
  const trimmed = String(text || "").trim();
  if (trimmed === "?") return { name: "?", args: "" };
  if (!trimmed.startsWith("/") || trimmed.includes("\n")) return null;
  const body = trimmed.slice(1);
  const separator = body.search(/\s/);
  if (separator < 0) return { name: body.toLowerCase(), args: "" };
  return {
    name: body.slice(0, separator).toLowerCase(),
    args: body.slice(separator).trim(),
  };
}

function splitFirstArgument(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return ["", ""];
  const separator = trimmed.search(/\s/);
  if (separator < 0) return [trimmed, ""];
  return [trimmed.slice(0, separator), trimmed.slice(separator).trim()];
}

function slashUsage(notice, usage) {
  notice(`Usage: ${usage}`);
  return { handled: true, clear: false };
}
