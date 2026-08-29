import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { slashMenuScrollDelta } from "./slash-menu-logic.js";

const MAX_ARGUMENT_ITEMS = 9;

export function useSlashCompletions({ text, enabled = true, sessionID = null, directory = "", hasSession = true }) {
  const commands = useStore((state) => state.commands);
  const context = useMemo(() => enabled ? slashCompletionContext(text) : null, [enabled, text]);
  const contextKey = completionContextKey(context);
  const [items, setItems] = useState([]);
  const [itemsKey, setItemsKey] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingKey, setLoadingKey] = useState("");

  useEffect(() => {
    let cancelled = false;
    setActiveIndex(0);
    if (!context) {
      setItems([]);
      setItemsKey("");
      setLoading(false);
      setLoadingKey("");
      return () => { cancelled = true; };
    }
    if (context.kind === "command") {
      const nextItems = commandCompletionItems(commands, context, { hasSession });
      setItems(nextItems);
      setItemsKey(contextKey);
      setActiveIndex(firstEnabledIndex(nextItems));
      setLoading(false);
      setLoadingKey("");
      return () => { cancelled = true; };
    }
    setLoading(true);
    setLoadingKey(contextKey);
    const request = context.kind === "skill"
      ? controller.slashSkillCompletions(directory, context.token)
      : controller.slashMcpCompletions(sessionID, directory, context.token);
    void request
      .then((data) => {
        if (!cancelled) {
          const nextItems = argumentCompletionItems(data, context, context.kind);
          setItems(nextItems);
          setItemsKey(contextKey);
          setActiveIndex(firstEnabledIndex(nextItems));
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setItems([]);
          controller.notice(error?.message || String(error));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
          setLoadingKey("");
        }
      });
    return () => { cancelled = true; };
  }, [commands, context, contextKey, directory, hasSession, sessionID]);

  const close = () => {
    setItems([]);
    setItemsKey("");
    setLoading(false);
    setLoadingKey("");
  };
  return {
    items: itemsKey === contextKey ? items : [],
    activeIndex,
    setActiveIndex,
    loading: loading && loadingKey === contextKey,
    context,
    close,
  };
}

export function slashCompletionContext(text) {
  const value = String(text || "");
  if (value.includes("\n")) return null;
  const leading = value.match(/^(\s*)(\/[^\s]*)(?:\s+(.*))?$/);
  if (!leading) return null;
  const commandToken = leading[2];
  const argumentText = leading[3];
  if (argumentText === undefined) {
    return {
      kind: "command",
      token: commandToken,
      replaceStart: leading[1].length,
      replaceEnd: value.length,
    };
  }
  const command = commandToken.toLowerCase();
  if (!["/skill", "/mcp"].includes(command)) return null;
  if (/\s/.test(argumentText)) return null;
  return {
    kind: command === "/skill" ? "skill" : "mcp",
    command,
    token: argumentText,
    replaceStart: value.length - argumentText.length,
    replaceEnd: value.length,
  };
}

export function commandCompletionItems(commands, context, { hasSession = true } = {}) {
  if (context?.kind !== "command") return [];
  const query = context.token.slice(1).toLowerCase();
  return commands
    .map((command, index) => ({ command, index, score: commandScore(command, query) }))
    .filter((entry) => entry.score < 99)
    .sort((left, right) => left.score - right.score || left.index - right.index)
    .map(({ command }) => {
      const name = `/${command.name}`;
      const requiresArgument = command.name === "skill" || command.name === "title";
      const unavailable = !hasSession && !["new", "shortcuts", "model", "image", "skill"].includes(command.name);
      return {
        id: `command:${command.name}`,
        kind: "command",
        label: commandUsage(command),
        description: unavailable ? "Available after a session starts." : command.description,
        value: `${name}${requiresArgument ? " " : ""}`,
        submitOnEnter: !requiresArgument && !unavailable,
        disabled: unavailable,
        replaceStart: context.replaceStart,
        replaceEnd: context.replaceEnd,
      };
    });
}

export function argumentCompletionItems(items, context, kind) {
  if (!context || context.kind !== kind) return [];
  const token = context.token.toLowerCase();
  return items
    .map((item) => ({ item, score: textScore(item.name, token) }))
    .filter((entry) => entry.score < 99)
    .sort((left, right) => left.score - right.score || left.item.name.localeCompare(right.item.name))
    .slice(0, MAX_ARGUMENT_ITEMS)
    .map(({ item }) => ({
      id: `${kind}:${item.name}`,
      kind,
      label: item.name,
      description: item.description || item.detail || "",
      value: item.name,
      submitOnEnter: true,
      disabled: false,
      replaceStart: context.replaceStart,
      replaceEnd: context.replaceEnd,
    }));
}

export function applySlashCompletion(text, item, { appendSpace = false } = {}) {
  const before = text.slice(0, item.replaceStart);
  const after = text.slice(item.replaceEnd);
  const suffix = appendSpace && !item.value.endsWith(" ") ? " " : "";
  const next = `${before}${item.value}${suffix}${after}`;
  return { text: next, cursor: before.length + item.value.length + suffix.length };
}

export function SlashCompletionMenu({ items, activeIndex, loading = false, onChoose, id = "slash-completion-menu" }) {
  const itemsRef = useRef(null);
  const [hasOverflow, setHasOverflow] = useState(false);
  const [moreBelow, setMoreBelow] = useState(false);
  const refreshScrollState = () => {
    const element = itemsRef.current;
    const overflow = Boolean(element && element.scrollHeight - element.clientHeight > 3);
    setHasOverflow(overflow);
    setMoreBelow(Boolean(overflow && element && element.scrollHeight - element.scrollTop - element.clientHeight > 3));
  };
  useEffect(() => {
    const frame = requestAnimationFrame(refreshScrollState);
    return () => cancelAnimationFrame(frame);
  }, [items.length, loading]);
  useLayoutEffect(() => {
    const element = itemsRef.current;
    const active = element?.querySelector(".slash-menu__item.is-active");
    if (!element || !active) return;
    const viewport = element.getBoundingClientRect();
    const item = active.getBoundingClientRect();
    const delta = slashMenuScrollDelta({
      viewportTop: viewport.top,
      viewportBottom: viewport.bottom,
      itemTop: item.top,
      itemBottom: item.bottom,
    });
    if (delta) element.scrollTop += delta;
    refreshScrollState();
  }, [activeIndex, items.length]);
  if (!items.length && !loading) return null;
  return html`<div id=${id} class="slash-menu" role="listbox" aria-label="Slash command completions">
    <div class="slash-menu__header">
      <span>${items[0]?.kind === "skill" ? "Skills" : items[0]?.kind === "mcp" ? "MCP servers" : "Commands"}</span>
      <span class="slash-menu__keys slash-menu__keys--desktop">↑↓ navigate · Tab complete · Enter choose · Esc close</span>
      <span class="slash-menu__keys slash-menu__keys--mobile">↑↓ Navigate · Tab Complete · Enter Choose · Esc Close</span>
    </div>
    <div ref=${itemsRef} class="slash-menu__items" onScroll=${refreshScrollState}>
      ${loading && !items.length ? html`<div class="slash-menu__loading">Loading completions…</div>` : items.map((item, index) => html`
        <button
          id=${`${id}-option-${index}`}
          type="button"
          role="option"
          aria-selected=${index === activeIndex}
          class=${`slash-menu__item ${index === activeIndex ? "is-active" : ""}`}
          disabled=${item.disabled}
          onMouseDown=${(event) => event.preventDefault()}
          onClick=${() => onChoose(item, { submit: false })}
        >
          <span class="slash-menu__copy"><strong>${item.label}</strong>${item.description ? html`<small>${item.description}</small>` : null}</span>
          ${item.disabled ? html`<span class="slash-menu__tag">session</span>` : null}
        </button>
      `)}
    </div>
    ${hasOverflow ? html`<div class="slash-menu__more" aria-hidden="true">${moreBelow ? "↓ More commands" : "End of commands"}</div>` : null}
  </div>`;
}

export function handleSlashMenuKey(event, menu, choose) {
  if (!menu.items.length && !menu.loading) return false;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    event.stopPropagation();
    if (!menu.items.length) return true;
    const delta = event.key === "ArrowDown" ? 1 : -1;
    menu.setActiveIndex((index) => nextEnabledIndex(menu.items, index, delta));
    return true;
  }
  if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    event.stopPropagation();
    if (menu.items.length) {
      menu.setActiveIndex(
        event.key === "Home" ? firstEnabledIndex(menu.items) : lastEnabledIndex(menu.items),
      );
    }
    return true;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    menu.close();
    return true;
  }
  if ((event.key === "Tab" && !event.shiftKey) || (event.key === "Enter" && !event.shiftKey)) {
    const item = menu.items[menu.activeIndex];
    if (!item && menu.loading) {
      event.preventDefault();
      event.stopPropagation();
      return true;
    }
    if (item?.disabled) {
      event.preventDefault();
      event.stopPropagation();
      return true;
    }
    if (!item) return false;
    event.preventDefault();
    event.stopPropagation();
    choose(item, { submit: event.key === "Enter" && item.submitOnEnter });
    return true;
  }
  return false;
}

function nextEnabledIndex(items, start, delta) {
  if (!items.length) return 0;
  let index = start;
  for (let attempts = 0; attempts < items.length; attempts += 1) {
    index = (index + delta + items.length) % items.length;
    if (!items[index].disabled) return index;
  }
  return start;
}

function firstEnabledIndex(items) {
  const index = items.findIndex((item) => !item.disabled);
  return index < 0 ? 0 : index;
}

function lastEnabledIndex(items) {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (!items[index].disabled) return index;
  }
  return Math.max(0, items.length - 1);
}

function completionContextKey(context) {
  if (!context) return "";
  return `${context.kind}:${context.command || ""}:${context.token}:${context.replaceStart}:${context.replaceEnd}`;
}

function commandUsage(command) {
  if (!command.usage) return `/${command.name}`;
  if (command.usage.startsWith("/")) return command.usage;
  return `/${command.name} ${command.usage}`;
}

function commandScore(command, query) {
  if (!query) return 0;
  const name = String(command.name || "").toLowerCase();
  const description = String(command.description || "").toLowerCase();
  const nameScore = textScore(name, query);
  if (nameScore < 99) return nameScore;
  return description.includes(query) ? 8 : 99;
}

function textScore(value, query) {
  if (!query) return 0;
  if (value === query) return 0;
  if (value.startsWith(query)) return 1;
  if (value.includes(query)) return 3;
  let cursor = 0;
  for (const character of value) {
    if (character === query[cursor]) cursor += 1;
    if (cursor === query.length) return 6;
  }
  return 99;
}
