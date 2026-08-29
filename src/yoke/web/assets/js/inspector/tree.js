import { html, useLayoutEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { displayTreeEntries, TREE_GRAPH_ROW_HEIGHT, treeGraphLayout } from "./tree-graph.js";
import { isTreeNavigationKey, treeKeyboardTarget } from "./tree-keyboard.js";

export function TreeInspector({ sessionID, data }) {
  const tree = data?.tree;
  const preview = data?.treePreview;
  const [summary, setSummary] = useState("");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [previewingID, setPreviewingID] = useState(null);
  const [navigating, setNavigating] = useState(false);
  const [showTechnical, setShowTechnical] = useState(false);
  const [focusedEntryID, setFocusedEntryID] = useState(null);
  const historyRef = useRef(null);
  const entryButtonRefs = useRef(new Map());
  const openedRef = useRef(null);
  const messageEntries = tree?.entries?.filter(isMessageEntry) || [];
  const visibleEntries = tree ? (showTechnical ? tree.entries : messageEntries) : [];
  const displayEntries = tree ? displayTreeEntries(tree.entries, visibleEntries) : [];
  const graph = treeGraphLayout(displayEntries);
  const nodeByID = new Map(graph.nodes.map((node) => [node.id, node]));
  const entryByID = new Map(displayEntries.map((entry) => [entry.id, entry]));
  const selectedEntry = preview ? entryByID.get(preview.targetID) || tree?.entries?.find((entry) => entry.id === preview.targetID) : null;
  const hiddenTechnicalCount = tree ? tree.entries.length - messageEntries.length : 0;
  const effectiveFocusID = focusedEntryID && entryByID.has(focusedEntryID)
    ? focusedEntryID
    : preview?.targetID && entryByID.has(preview.targetID)
      ? preview.targetID
      : displayEntries.find((entry) => entry.current)?.id || displayEntries.at(-1)?.id || null;

  useLayoutEffect(() => {
    const node = historyRef.current;
    if (!node || !displayEntries.length || openedRef.current === sessionID) return;
    openedRef.current = sessionID;
    const focusID = displayEntries.find((entry) => entry.current)?.id || displayEntries.at(-1)?.id || null;
    requestAnimationFrame(() => {
      scrollToHead(node, { behavior: "auto" });
      const button = focusID ? entryButtonRefs.current.get(focusID) : null;
      if (!button) return;
      button.focus({ preventScroll: true });
      keepTreeEntryVisible(node, button);
    });
  }, [sessionID, displayEntries.length]);

  useLayoutEffect(() => {
    if (!preview?.targetID || !effectiveFocusID) return;
    const frame = requestAnimationFrame(() => {
      const button = entryButtonRefs.current.get(effectiveFocusID);
      if (button) keepTreeEntryVisible(historyRef.current, button);
    });
    return () => cancelAnimationFrame(frame);
  }, [preview?.targetID, effectiveFocusID]);

  if (!tree) return html`<div class="inspector-loading">Loading conversation graph…</div>`;

  const loadOlder = async () => {
    if (loadingOlder) return;
    const scroller = historyRef.current;
    const beforeHeight = scroller?.scrollHeight || 0;
    const beforeTop = scroller?.scrollTop || 0;
    setLoadingOlder(true);
    try {
      await controller.loadMoreTree(sessionID);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (!scroller) return;
        scroller.scrollTop = beforeTop + Math.max(0, scroller.scrollHeight - beforeHeight);
      }));
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setLoadingOlder(false);
    }
  };

  const previewEntry = async (entryID) => {
    if (previewingID || navigating) return;
    if (entryID === tree.leafID) {
      controller.clearTreePreview(sessionID);
      return;
    }
    setPreviewingID(entryID);
    setSummary("");
    try {
      await controller.treePreview(sessionID, entryID);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setPreviewingID(null);
    }
  };

  const navigateToPreview = async () => {
    if (!preview || preview.current || navigating) return;
    setNavigating(true);
    try {
      const result = await controller.navigateTree(sessionID, preview.targetID, summary || null);
      if (result) {
        setSummary("");
        requestAnimationFrame(() => requestAnimationFrame(() => scrollToHead(historyRef.current, { behavior: "smooth" })));
      }
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setNavigating(false);
    }
  };

  const toggleTechnical = (next) => {
    const scroller = historyRef.current;
    const head = scroller?.querySelector(".tree-entry.is-current");
    const headOffset = head && scroller ? head.getBoundingClientRect().top - scroller.getBoundingClientRect().top : null;
    setShowTechnical(next);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!scroller) return;
      const nextHead = scroller.querySelector(".tree-entry.is-current");
      if (nextHead && headOffset != null) {
        scroller.scrollTop += nextHead.getBoundingClientRect().top - scroller.getBoundingClientRect().top - headOffset;
      }
    }));
  };

  const focusTreeEntry = (entryID) => {
    if (!entryID) return;
    setFocusedEntryID(entryID);
    requestAnimationFrame(() => {
      const button = entryButtonRefs.current.get(entryID);
      if (!button) return;
      button.focus({ preventScroll: true });
      keepTreeEntryVisible(historyRef.current, button);
    });
  };

  const onEntryKeyDown = (event, entry) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void previewEntry(entry.id);
      return;
    }
    if (event.key === "Escape" && preview) {
      event.preventDefault();
      event.stopPropagation();
      controller.clearTreePreview(sessionID);
      return;
    }
    if (!isTreeNavigationKey(event.key)) return;
    event.preventDefault();
    const targetID = treeKeyboardTarget(displayEntries, entry.id, event.key);
    if (targetID) focusTreeEntry(targetID);
  };

  return html`<div class=${`tree-inspector ${preview ? "has-preview" : ""}`}>
    <div class="tree-toolbar">
      <div class="tree-toolbar__identity">
        <div class="tree-toolbar__title"><span class="tree-head-dot"></span>Conversation HEAD</div>
        <div class="tree-toolbar__count" title=${`Tree revision ${tree.revision}`}>
          ${messageEntries.length} messages · ${tree.totalEntries || tree.entries.length} total nodes
        </div>
      </div>
      <div class="tree-toolbar__actions">
        <span class="tree-keyboard-hint"><kbd>↑</kbd><kbd>↓</kbd> navigate · <kbd>←</kbd> parent · <kbd>→</kbd> child · <kbd>Enter</kbd> target</span>
        <div class="tree-mode-toggle" role="group" aria-label="Tree node visibility">
          <button class=${!showTechnical ? "is-active" : ""} aria-pressed=${!showTechnical} onClick=${() => toggleTechnical(false)}>Messages</button>
          <button class=${showTechnical ? "is-active" : ""} aria-pressed=${showTechnical} onClick=${() => toggleTechnical(true)}>All nodes${hiddenTechnicalCount ? html` <span>${hiddenTechnicalCount}</span>` : null}</button>
        </div>
        <button class="tree-jump-head" onClick=${() => scrollToHead(historyRef.current, { behavior: "smooth" })}>↓ HEAD</button>
        ${tree.cursor?.next ? html`
          <button class="secondary-action tree-load-more" disabled=${loadingOlder || navigating} onClick=${loadOlder}>
            ${loadingOlder ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
            <span>${loadingOlder ? "Loading" : "Older"}</span>
          </button>
        ` : null}
      </div>
    </div>

    <div class="tree-workspace">
      <section class="tree-history" ref=${historyRef} aria-label="Conversation graph">
        <div class="tree-history__columns" style=${`--tree-graph-width:${graph.graphWidth}px`}>
          <span>Graph</span><span>Conversation</span><span>When</span>
        </div>
        ${displayEntries.length ? html`<div class="tree-graph-rows" style=${`--tree-graph-width:${graph.graphWidth}px;--tree-row-height:${TREE_GRAPH_ROW_HEIGHT}px`}>
          <svg class="tree-graph-canvas" width=${graph.graphWidth} height=${graph.height} viewBox=${`0 0 ${graph.graphWidth} ${graph.height}`} aria-hidden="true">
            ${graph.nodes.filter((node) => node.externalParent).map((node) => html`
              <path class=${`tree-graph-edge tree-lane-color--${node.lane % 6}`} d=${`M ${node.x} ${Math.max(0, node.y - TREE_GRAPH_ROW_HEIGHT / 2)} L ${node.x} ${node.y}`} />
            `)}
            ${graph.edges.map((edge) => html`<path key=${`${edge.parentID}:${edge.childID}`} class=${`tree-graph-edge tree-lane-color--${edge.lane % 6} ${edge.active ? "is-active" : "is-abandoned"}`} d=${edge.path} />`)}
            ${graph.nodes.map((node) => html`
              <g key=${node.id} class=${`tree-graph-node tree-lane-color--${node.lane % 6} ${node.current ? "is-current" : ""} ${node.active ? "is-active" : "is-abandoned"} ${preview?.targetID === node.id ? "is-target" : ""}`}>
                ${node.current ? html`<circle class="tree-graph-node__halo" cx=${node.x} cy=${node.y} r="9"></circle>` : null}
                ${preview?.targetID === node.id && !node.current ? html`<circle class="tree-graph-node__target" cx=${node.x} cy=${node.y} r="8"></circle>` : null}
                <circle class="tree-graph-node__dot" cx=${node.x} cy=${node.y} r=${node.current ? 4.8 : 3.7}></circle>
              </g>
            `)}
          </svg>
          <div class="tree-list" role="list" aria-label="Conversation tree nodes">
            ${displayEntries.map((entry) => {
              const previewing = previewingID === entry.id;
              const selected = preview?.targetID === entry.id;
              const kind = !showTechnical && entry.kind === "assistant_tool_calls" ? "assistant" : entry.kind;
              const graphNode = nodeByID.get(entry.id);
              return html`<div
                key=${entry.id}
                role="listitem"
                class=${`tree-entry ${entry.current ? "is-current" : ""} ${entry.active ? "is-active" : "is-abandoned"} ${selected ? "is-selected" : ""} ${effectiveFocusID === entry.id ? "is-keyboard-focus" : ""}`}
              >
                <div class="tree-entry__graph-cell" aria-hidden="true"></div>
                <button
                  ref=${(node) => {
                    if (node) entryButtonRefs.current.set(entry.id, node);
                    else entryButtonRefs.current.delete(entry.id);
                  }}
                  class="tree-entry__summary"
                  disabled=${navigating}
                  aria-busy=${previewing ? "true" : null}
                  tabindex=${effectiveFocusID === entry.id ? 0 : -1}
                  aria-current=${entry.current ? "true" : null}
                  aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Home End PageUp PageDown Enter Space Escape"
                  title=${entry.current ? "Current conversation HEAD" : "Select as a possible conversation HEAD"}
                  onFocus=${() => setFocusedEntryID(entry.id)}
                  onKeyDown=${(event) => onEntryKeyDown(event, entry)}
                  onClick=${() => previewEntry(entry.id)}
                >
                  <span class="tree-entry__identity">
                    <span class="tree-kind">${kindLabel(kind)}</span>
                    ${entry.current ? html`<span class="tree-ref tree-ref--head">HEAD</span>` : selected ? html`<span class="tree-ref tree-ref--target">TARGET</span>` : entry.active ? html`<span class="tree-ref">current path</span>` : html`<span class=${`tree-ref tree-lane-color--${graphNode?.lane % 6 || 0}`}>branch</span>`}
                    ${entry.label ? html`<span class="tree-label">${entry.label}</span>` : null}
                    ${previewing ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
                  </span>
                  <span class="tree-preview-text">${entry.preview || technicalPreview(entry)}</span>
                </button>
                <div class="tree-entry__right">
                  <time title=${entry.createdAt || ""}>${formatTreeTime(entry.createdAt)}</time>
                  <button class="tree-label-action" title="Label this node" disabled=${navigating} onClick=${() => {
                    const label = window.prompt("Node label", entry.label || "");
                    if (label !== null) void controller.labelTreeEntry(sessionID, entry.id, label);
                  }}>Label</button>
                </div>
              </div>`;
            })}
          </div>
        </div>` : html`<div class="tree-empty">
          No user or assistant messages in this loaded window.${tree.cursor?.next ? " Load older nodes to continue." : ""}
        </div>`}
      </section>

      ${preview ? html`<aside class="navigation-preview">
        <div class="navigation-preview__head">
          <div>
            <div class="inspector-section-title">Move conversation HEAD</div>
            <div class="navigation-preview__target">Future prompts will continue from the selected node.</div>
          </div>
          <button class="navigation-preview__close" title="Clear target" onClick=${() => controller.clearTreePreview(sessionID)}>×</button>
        </div>

        <div class="navigation-target-card">
          <div class="navigation-target-card__meta">
            <span class="tree-ref tree-ref--target">TARGET</span>
            <span>${kindLabel(selectedEntry?.kind || "node")}</span>
            ${selectedEntry?.label ? html`<span>${selectedEntry.label}</span>` : null}
          </div>
          <strong>${selectedEntry?.preview || selectedEntry?.id || preview.targetID}</strong>
        </div>

        ${preview.abandonedTotal ? html`<div class="navigation-impact navigation-impact--warn">
          <strong>${preview.abandonedTotal} active ${preview.abandonedTotal === 1 ? "node" : "nodes"} will become abandoned</strong>
          <span>Nothing is deleted. The old path remains visible in the graph and can be checked out again later.</span>
        </div>` : html`<div class="navigation-impact">
          <strong>No active work will be abandoned</strong>
          <span>HEAD moves directly to this point in the current path.</span>
        </div>`}

        ${preview.editorText ? html`<div class="navigation-restored-editor"><span class="field-label">Prompt restored to composer</span><pre>${preview.editorText}</pre></div>` : null}

        ${preview.abandoned?.length ? html`<details class="navigation-abandoned">
          <summary>Review abandoned path${preview.abandonedTruncated ? ` · showing ${preview.abandoned.length}/${preview.abandonedTotal}` : ` · ${preview.abandoned.length}`}</summary>
          <ul>${preview.abandoned.map((item) => html`<li><strong>${kindLabel(item.kind)}</strong><span>${item.preview || item.id}</span></li>`)}</ul>
        </details>` : null}

        ${preview.abandonedTotal ? html`<label class="stacked-label navigation-summary">Handoff note for the branch you leave <span>optional</span><textarea rows="3" value=${summary} disabled=${navigating} placeholder="Preserve anything the new branch should remember…" onInput=${(event) => setSummary(event.currentTarget.value)}></textarea></label>` : null}

        <div class="navigation-preview__footer">
          <button onClick=${() => controller.clearTreePreview(sessionID)} disabled=${navigating}>Cancel</button>
          <button class="primary navigation-preview__action" disabled=${preview.current || navigating} onClick=${navigateToPreview}>
            ${navigating ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
            <span>${navigating ? "Moving HEAD" : "Move HEAD here"}</span>
          </button>
        </div>
      </aside>` : null}
    </div>
  </div>`;
}

function scrollToHead(scroller, { behavior = "smooth" } = {}) {
  if (!scroller) return;
  const head = scroller.querySelector(".tree-entry.is-current");
  if (head) {
    head.scrollIntoView({ block: "center", behavior });
    return;
  }
  scroller.scrollTo({ top: scroller.scrollHeight, behavior });
}

function keepTreeEntryVisible(scroller, button) {
  if (!scroller || !button) return;
  const row = button.closest(".tree-entry") || button;
  const scrollerRect = scroller.getBoundingClientRect();
  const columnsBottom = scroller.querySelector(".tree-history__columns")?.getBoundingClientRect().bottom || scrollerRect.top;
  const rowRect = row.getBoundingClientRect();
  const visibleTop = Math.max(scrollerRect.top, columnsBottom);
  const visibleBottom = scrollerRect.bottom;
  if (rowRect.top < visibleTop) scroller.scrollTop -= visibleTop - rowRect.top;
  else if (rowRect.bottom > visibleBottom) scroller.scrollTop += rowRect.bottom - visibleBottom;
}

function isMessageEntry(entry) {
  if (entry.kind === "user" || entry.kind === "assistant") return true;
  return entry.kind === "assistant_tool_calls" && Boolean(entry.preview);
}

function kindLabel(kind) {
  const normalized = String(kind || "node").replace(/^assistant_tool_calls$/, "assistant").replace(/_/g, " ");
  if (normalized === "user") return "You";
  if (normalized === "assistant") return "Assistant";
  return normalized;
}

function technicalPreview(entry) {
  return entry.label || `${kindLabel(entry.kind)} · ${entry.id}`;
}

function formatTreeTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
