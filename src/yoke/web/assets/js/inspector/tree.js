import { html, useLayoutEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { store } from "../state/store.js";
import { useInspectorPreferences } from "./state/preferences.js";
import { treeGraphLayout } from "./tree-graph.js";
import { treeKeyboardAction, treeKeyboardTarget } from "./tree-keyboard.js";
import { TreeDestination } from "./tree/destination.js";
import { TreeDetail } from "./tree/detail.js";
import { TreeHistory, keepTreeEntryVisible } from "./tree/history.js";
import { nextTreeMatch, treeMatches, treeView } from "./tree/model.js";

const DEFAULTS = { showTechnical: false, search: "", scrollTop: null, mobilePane: "list", selectedID: null };

export function TreeInspector({ sessionID, data }) {
  return html`<${TreeSession} key=${sessionID} sessionID=${sessionID} data=${data} />`;
}

function TreeSession({ sessionID, data }) {
  const tree = data?.tree;
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "tree", DEFAULTS);
  const [, render] = useState(0);
  const [summary, setSummary] = useState("");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [revealing, setRevealing] = useState(false);
  const [focusedID, setFocusedID] = useState(null);
  const historyRef = useRef(null);
  const buttonRefs = useRef(new Map());
  const openedRef = useRef(null);
  const focusRef = useRef(null);
  const jumpGeneration = useRef(0);
  const aliveRef = useRef(true);
  const latestRef = useRef(null);
  const destination = useMemo(() => new TreeDestination({
    preview: (id) => controller.treePreview(sessionID, id),
    clearPreview: () => controller.clearTreePreview(sessionID),
    navigate: (id, note) => controller.navigateTree(sessionID, id, note),
    changed: () => render((value) => value + 1),
  }), [sessionID]);
  const view = useMemo(() => treeView(tree, preferences.showTechnical), [tree, preferences.showTechnical]);
  const graph = useMemo(() => treeGraphLayout(view.rows), [view.rows]);
  const matches = useMemo(() => treeMatches(view.rows, preferences.search), [view.rows, preferences.search]);
  const state = destination.state;
  const selected = view.rows.find((entry) => entry.id === state.targetID) || view.entries.find((entry) => entry.id === state.targetID);
  const parent = selected ? view.entries.find((entry) => entry.id === (selected.graphParentID || selected.parentID)) : null;
  const focusID = view.rows.some((entry) => entry.id === focusedID) ? focusedID
    : view.rows.find((entry) => entry.current)?.id || view.rows.at(-1)?.id;
  const ready = Boolean(selected && destination.ready(tree, data?.treePreview));
  latestRef.current = { tree, sharedPreview: data?.treePreview, summary, view };

  const focusEntry = (id, { focus = true } = {}) => {
    if (!id) return;
    focusRef.current = id;
    setFocusedID(id);
    requestAnimationFrame(() => {
      if (!aliveRef.current || focusRef.current !== id) return;
      const button = buttonRefs.current.get(id);
      if (focus) button?.focus({ preventScroll: true });
      keepTreeEntryVisible(historyRef.current, button);
    });
  };

  const choose = (id, { detail = true, focus = true } = {}) => {
    if (!id || destination.state.moving) return;
    ++jumpGeneration.current;
    setRevealing(false);
    setSummary("");
    patchPreferences({ selectedID: id, ...(detail ? { mobilePane: "detail" } : {}) });
    focusEntry(id, { focus });
    void destination.select(id, latestRef.current.tree);
  };

  const clear = () => {
    if (destination.state.moving) return;
    ++jumpGeneration.current;
    setRevealing(false);
    destination.clear();
    setSummary("");
    patchPreferences({ selectedID: null, mobilePane: "list" });
    focusEntry(focusRef.current || focusID);
  };

  const continueHere = async (event) => {
    const current = store.getState();
    if (current.ui.selectedSessionID !== sessionID || current.ui.inspector?.mode !== "tree") return;
    const currentData = current.sessionData[sessionID];
    if (!currentData?.tree?.entries.some((entry) => entry.id === destination.state.targetID)) return;
    const result = await destination.continue(currentData?.tree, currentData?.treePreview, latestRef.current.summary, { repeat: Boolean(event?.repeat) });
    if (!result || !aliveRef.current) return;
    setSummary("");
    patchPreferences({ selectedID: null, mobilePane: "list" });
    focusEntry(latestRef.current.tree?.leafID);
  };

  useLayoutEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      ++jumpGeneration.current;
      destination.dispose();
    };
  }, [destination]);

  useLayoutEffect(() => {
    if (preferences.mobilePane !== "detail" || !selected || !window.matchMedia("(max-width: 820px)").matches) return;
    const frame = requestAnimationFrame(() => historyRef.current?.parentElement?.querySelector(".tree-detail__back")?.focus());
    return () => cancelAnimationFrame(frame);
  }, [preferences.mobilePane, Boolean(selected)]);

  // A revision change invalidates even a completed preview. Fetch again, never
  // silently authorize the new revision using consequences from the old one.
  useLayoutEffect(() => {
    if (!tree || !state.targetID || state.moving || state.revision === tree.revision) return;
    if (!tree.entries.some((entry) => entry.id === state.targetID)) {
      clear();
      return;
    }
    void destination.select(state.targetID, tree);
  }, [tree?.revision, state.targetID, state.moving, destination]);

  useLayoutEffect(() => {
    if (!tree || openedRef.current === sessionID) return;
    openedRef.current = sessionID;
    const generation = jumpGeneration.current;
    const openingFocus = document.activeElement;
    const restore = async () => {
      try {
        const loaded = await controller.revealTreeHead(sessionID);
        if (!aliveRef.current || generation !== jumpGeneration.current || !loaded) return;
        const id = preferences.selectedID && loaded.entries.some((entry) => entry.id === preferences.selectedID)
          ? preferences.selectedID : loaded.leafID;
        if (preferences.selectedID && id === preferences.selectedID) void destination.select(id, loaded);
        focusEntry(id, { focus: document.activeElement === openingFocus });
        requestAnimationFrame(() => {
          if (aliveRef.current && generation === jumpGeneration.current && preferences.scrollTop != null && historyRef.current) {
            historyRef.current.scrollTop = preferences.scrollTop;
          }
        });
      } catch (error) {
        if (aliveRef.current) controller.notice(error?.message || String(error));
      }
    };
    void restore();
  }, [sessionID, Boolean(tree)]);

  const jumpCurrent = async () => {
    if (destination.state.moving) return;
    const generation = ++jumpGeneration.current;
    destination.clear();
    setSummary("");
    patchPreferences({ selectedID: null, mobilePane: "list" });
    setRevealing(true);
    try {
      const loaded = await controller.revealTreeHead(sessionID);
      if (!aliveRef.current || generation !== jumpGeneration.current || !loaded) return;
      patchPreferences({ mobilePane: "list", selectedID: loaded.leafID });
      setSummary("");
      focusEntry(loaded.leafID);
      void destination.select(loaded.leafID, loaded);
    } catch (error) {
      if (aliveRef.current && generation === jumpGeneration.current) controller.notice(error?.message || String(error));
    } finally {
      if (aliveRef.current && generation === jumpGeneration.current) setRevealing(false);
    }
  };

  const loadOlder = async () => {
    if (loadingOlder) return;
    const scroller = historyRef.current;
    const beforeHeight = scroller?.scrollHeight || 0;
    const beforeTop = scroller?.scrollTop || 0;
    const generation = jumpGeneration.current;
    setLoadingOlder(true);
    try {
      await controller.loadMoreTree(sessionID);
      requestAnimationFrame(() => {
        if (!aliveRef.current || !scroller) return;
        if (generation === jumpGeneration.current) scroller.scrollTop = beforeTop + Math.max(0, scroller.scrollHeight - beforeHeight);
        else keepTreeEntryVisible(scroller, buttonRefs.current.get(focusRef.current));
      });
    } catch (error) {
      if (aliveRef.current) controller.notice(error?.message || String(error));
    } finally {
      if (aliveRef.current) setLoadingOlder(false);
    }
  };

  const toggleTechnical = (showTechnical) => {
    const anchor = buttonRefs.current.get(focusID)?.closest(".tree-entry");
    const top = anchor?.getBoundingClientRect().top;
    if (state.targetID && !treeView(tree, showTechnical).rows.some((entry) => entry.id === state.targetID)) clear();
    patchPreferences({ showTechnical });
    requestAnimationFrame(() => {
      const next = buttonRefs.current.get(focusID)?.closest(".tree-entry");
      if (next && top != null && historyRef.current) historyRef.current.scrollTop += next.getBoundingClientRect().top - top;
    });
  };

  const onEntryKeyDown = (event, entry) => {
    const action = treeKeyboardAction(event);
    if (!action || (action === "clear" && !destination.state.targetID)) return;
    event.preventDefault();
    event.stopPropagation();
    if (action === "clear") clear();
    if (action === "continue" && destination.state.targetID === entry.id) void continueHere(event);
    if (action === "choose") choose(entry.id);
    if (action === "select") {
      const id = treeKeyboardTarget(view.rows, focusRef.current || entry.id, event.key);
      choose(id, { detail: false, focus: true });
    }
  };

  const find = (direction) => {
    const id = nextTreeMatch(matches, destination.state.targetID, direction);
    if (id) choose(id, { detail: false, focus: false });
  };

  if (!tree) return html`<div class="inspector-loading">Loading conversation graph…</div>`;
  return html`<div class=${`tree-inspector ${preferences.mobilePane === "detail" && selected ? "is-detail" : ""}`} onKeyDown=${(event) => {
    if (event.key === "Escape" && destination.state.targetID && !event.defaultPrevented) {
      event.preventDefault();
      event.stopPropagation();
      clear();
    }
  }}>
    <div class="tree-toolbar">
      <div class="tree-toolbar__actions">
        <div class="tree-mode-toggle" role="group" aria-label="Tree node visibility">
          <button disabled=${state.moving} class=${!preferences.showTechnical ? "is-active" : ""} aria-pressed=${!preferences.showTechnical} onClick=${() => toggleTechnical(false)}>Messages</button>
          <button disabled=${state.moving} class=${preferences.showTechnical ? "is-active" : ""} aria-pressed=${preferences.showTechnical} onClick=${() => toggleTechnical(true)}>All nodes</button>
        </div>
        <button disabled=${state.moving} onClick=${jumpCurrent}>${revealing ? "Finding current…" : "Current"}</button>
        <button disabled=${state.moving || !view.latestID} onClick=${() => {
          // Latest may itself be technical. Reveal it without disguising it as a message.
          if (!view.rows.some((entry) => entry.id === view.latestID)) patchPreferences({ showTechnical: true });
          choose(view.latestID, { detail: false, focus: true });
          patchPreferences({ mobilePane: "list" });
        }}>Latest</button>
        ${selected ? html`<button class="tree-open-detail" onClick=${() => patchPreferences({ mobilePane: "detail" })}>Review destination</button>` : null}
        ${tree.cursor?.next ? html`<button disabled=${loadingOlder || state.moving} onClick=${loadOlder}>${loadingOlder ? "Loading older…" : "Older"}</button>` : null}
        <details class="tree-help"><summary>Keyboard help</summary><div>
          <p><kbd>↑</kbd> <kbd>↓</kbd> select a destination. <kbd>←</kbd> parent, <kbd>→</kbd> child.</p>
          <p><kbd>Home</kbd> / <kbd>End</kbd> first / last loaded row. <kbd>Page Up</kbd> / <kbd>Page Down</kbd> skip five rows.</p>
          <p><kbd>Enter</kbd> continues only after the selected preview loads. <kbd>Space</kbd> selects. <kbd>Escape</kbd> clears.</p>
          <p>Brighter lines mark active context. The double-ring node is current HEAD. Latest is the last chronological event, which may be on another branch.</p>
        </div></details>
      </div>
      <div class="tree-find">
        <input type="search" aria-label="Find message, label or node ID in loaded rows" placeholder="Find message or label" value=${preferences.search} onInput=${(event) => patchPreferences({ search: event.currentTarget.value })} onKeyDown=${(event) => {
          if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); if (!event.repeat) find(event.shiftKey ? -1 : 1); }
        }} />
        ${preferences.search.trim() ? html`<span role="status">${matches.length} ${matches.length === 1 ? "match" : "matches"}</span><button disabled=${!matches.length || state.moving} onClick=${() => find(-1)} aria-label="Previous match">↑</button><button disabled=${!matches.length || state.moving} onClick=${() => find(1)} aria-label="Next match">↓</button>` : null}
      </div>
      <p class="tree-toolbar__count" title=${`${view.messageCount} messages, ${view.hiddenCount} hidden technical nodes${view.anchorCount ? ", current technical position retained" : ""}. Search covers shown rows only.`}>
        <span>Oldest to newest</span>
        <span>${view.rows.length} shown${tree.entries.length < (tree.totalEntries ?? tree.entries.length) ? ` / ${tree.entries.length} loaded / ${tree.totalEntries} total` : ` / ${tree.totalEntries ?? tree.entries.length} nodes`}${preferences.search.trim() ? " · Search shown rows" : ""}</span>
      </p>
    </div>
    <div class="tree-workspace">
      <${TreeHistory} graph=${graph} rows=${view.rows} latestID=${view.latestID} selectedID=${state.targetID} focusID=${focusID} matches=${matches}
        pending=${state.pending} moving=${state.moving} historyRef=${historyRef} buttonRefs=${buttonRefs} onSelect=${choose}
        onFocus=${(id) => { focusRef.current = id; setFocusedID(id); }} onKeyDown=${onEntryKeyDown}
        onScroll=${(event) => patchPreferences({ scrollTop: event.currentTarget.scrollTop })} />
      <${TreeDetail} sessionID=${sessionID} entry=${selected} parent=${parent} messages=${data?.messages} state=${{ ...state, preview: state.revision === tree.revision ? state.preview : null }} ready=${ready}
        summary=${summary} setSummary=${setSummary} onContinue=${continueHere} onClear=${clear} onRetry=${() => destination.select(state.targetID, tree)}
        onBack=${() => { patchPreferences({ mobilePane: "list" }); focusEntry(state.targetID || focusID); }} />
    </div>
  </div>`;
}
