import { html, useEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { trapFocus } from "../lib/focus.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";

export function CommandPalette() {
  const open = useStore((state) => state.ui.commandPaletteOpen);
  const commands = useStore((state) => state.commands);
  const results = useStore((state) => state.ui.searchResults);
  const sessions = useStore((state) => state.sessions);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const dialog = useRef(null);
  const input = useRef(null);
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    requestAnimationFrame(() => input.current?.focus());
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const id = setTimeout(() => controller.searchSessions(query), 120);
    return () => clearTimeout(id);
  }, [query, open]);
  const filteredCommands = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((command) => `${command.name} ${command.description}`.toLowerCase().includes(needle));
  }, [commands, query]);
  const sessionResults = results.slice(0, 8);
  const totalResults = sessionResults.length + filteredCommands.length;
  useEffect(() => {
    if (!open) return;
    setActiveIndex(0);
  }, [query, open]);
  useEffect(() => {
    if (!open || !totalResults) return;
    const next = Math.min(activeIndex, totalResults - 1);
    if (next !== activeIndex) setActiveIndex(next);
    requestAnimationFrame(() => dialog.current?.querySelector(`[data-palette-index="${next}"]`)?.scrollIntoView({ block: "nearest" }));
  }, [activeIndex, totalResults, open]);
  if (!open) return null;
  const activate = (index) => {
    if (index < sessionResults.length) {
      controller.togglePalette(false);
      controller.selectSession(sessionResults[index]);
      return;
    }
    const command = filteredCommands[index - sessionResults.length];
    if (command) controller.runPaletteCommand(command).catch?.((error) => controller.notice(error?.message || String(error)));
  };
  const onKeyDown = (event) => {
    if (event.key === "ArrowDown" && totalResults) {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % totalResults);
      return;
    }
    if (event.key === "ArrowUp" && totalResults) {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + totalResults) % totalResults);
      return;
    }
    if (event.key === "Enter" && totalResults) {
      event.preventDefault();
      activate(activeIndex);
      return;
    }
    trapFocus(dialog.current, event);
  };
  return html`<div class="modal-backdrop" onMouseDown=${(event) => event.target === event.currentTarget && controller.togglePalette(false)}>
    <div class="command-palette" role="dialog" aria-modal="true" aria-label="Command palette" ref=${dialog} onKeyDown=${onKeyDown}>
      <div class="command-search"><span>⌕</span><input ref=${input} value=${query} placeholder="Search sessions or commands" onInput=${(event) => setQuery(event.currentTarget.value)} /></div>
      <div class="command-results">
        ${sessionResults.length ? html`<div class="command-group"><div class="command-group__label">Sessions</div>${sessionResults.map((id, index) => html`<button class=${activeIndex === index ? "is-active" : ""} data-palette-index=${index} onMouseEnter=${() => setActiveIndex(index)} onClick=${() => activate(index)}><span><strong>${sessions[id]?.title || id}</strong><small>${sessions[id]?.location?.directory || ""}</small></span><span>↵</span></button>`)}</div>` : null}
        <div class="command-group"><div class="command-group__label">Commands</div>${filteredCommands.map((command, commandIndex) => {
          const index = sessionResults.length + commandIndex;
          return html`<button class=${activeIndex === index ? "is-active" : ""} data-palette-index=${index} onMouseEnter=${() => setActiveIndex(index)} onClick=${() => activate(index)}><span><strong>/${command.name}</strong><small>${command.description}</small></span><span>${command.usage || ""}</span></button>`;
        })}</div>
      </div>
    </div>
  </div>`;
}
