import { html, useLayoutEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { displayTreeEntries, TREE_GRAPH_ROW_HEIGHT, treeGraphLayout } from "./tree-graph.js";

export function TreeInspector({ sessionID, data }) {
  const tree = data?.tree;
  const preview = data?.treePreview;
  const [summary, setSummary] = useState("");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [previewingID, setPreviewingID] = useState(null);
  const [navigating, setNavigating] = useState(false);
  const [showTechnical, setShowTechnical] = useState(false);
  const historyRef = useRef(null);
  const openedAtLatestRef = useRef(false);
  const messageEntries = tree?.entries?.filter(isMessageEntry) || [];
  const visibleEntries = tree ? (showTechnical ? tree.entries : messageEntries) : [];
  const displayEntries = tree ? displayTreeEntries(tree.entries, visibleEntries) : [];
  const graph = treeGraphLayout(displayEntries);
  const nodeByID = new Map(graph.nodes.map((node) => [node.id, node]));
  const hiddenTechnicalCount = tree ? tree.entries.length - messageEntries.length : 0;

  useLayoutEffect(() => {
    const node = historyRef.current;
    if (!node || openedAtLatestRef.current || !displayEntries.length) return;
    node.scrollTop = node.scrollHeight;
    openedAtLatestRef.current = true;
  }, [sessionID, displayEntries.length]);

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
    setPreviewingID(entryID);
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
      await controller.navigateTree(sessionID, preview.targetID, summary || null);
      setSummary("");
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setNavigating(false);
    }
  };

  const toggleTechnical = () => {
    const scroller = historyRef.current;
    const wasNearLatest = scroller
      ? scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100
      : true;
    setShowTechnical((value) => !value);
    if (wasNearLatest) requestAnimationFrame(() => requestAnimationFrame(() => {
      if (historyRef.current) historyRef.current.scrollTop = historyRef.current.scrollHeight;
    }));
  };

  return html`<div class=${`tree-inspector ${preview ? "has-preview" : ""}`}>
    <div class="tree-toolbar">
      <div>
        <div class="tree-toolbar__title">Conversation history</div>
        <div class="tree-toolbar__count" title=${`Tree revision ${tree.revision}`}>
          ${showTechnical
            ? `${tree.entries.length} loaded nodes`
            : `${messageEntries.length} messages${hiddenTechnicalCount ? ` · ${hiddenTechnicalCount} technical hidden` : ""}`}
        </div>
      </div>
      <div class="tree-toolbar__actions">
        ${hiddenTechnicalCount || showTechnical ? html`
          <button class="tree-view-toggle" disabled=${navigating} onClick=${toggleTechnical}>
            ${showTechnical ? "Messages only" : "Show all nodes"}
          </button>
        ` : null}
        ${tree.cursor?.next ? html`
          <button class="secondary-action tree-load-more" disabled=${loadingOlder || navigating} onClick=${loadOlder}>
            ${loadingOlder ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
            <span>${loadingOlder ? "Loading older" : "Load older"}</span>
          </button>
        ` : null}
      </div>
    </div>

    <div class="tree-workspace">
      <section class="tree-history" ref=${historyRef} aria-label="Conversation graph">
        <div class="tree-history__columns" style=${`--tree-graph-width:${graph.graphWidth}px`}>
          <span>Graph</span><span>Description</span><span>State</span>
        </div>
        ${displayEntries.length ? html`<div class="tree-graph-rows" style=${`--tree-graph-width:${graph.graphWidth}px;--tree-row-height:${TREE_GRAPH_ROW_HEIGHT}px`}>
          <svg class="tree-graph-canvas" width=${graph.graphWidth} height=${graph.height} viewBox=${`0 0 ${graph.graphWidth} ${graph.height}`} aria-hidden="true">
            ${graph.nodes.filter((node) => node.externalParent).map((node) => html`
              <path class=${`tree-graph-edge tree-lane-color--${node.lane % 6}`} d=${`M ${node.x} ${Math.max(0, node.y - TREE_GRAPH_ROW_HEIGHT / 2)} L ${node.x} ${node.y}`} />
            `)}
            ${graph.edges.map((edge) => html`<path key=${`${edge.parentID}:${edge.childID}`} class=${`tree-graph-edge tree-lane-color--${edge.lane % 6}`} d=${edge.path} />`)}
            ${graph.nodes.map((node) => html`
              <g key=${node.id} class=${`tree-graph-node tree-lane-color--${node.lane % 6} ${node.current ? "is-current" : ""} ${node.active ? "is-active" : "is-abandoned"}`}>
                ${node.current ? html`<circle class="tree-graph-node__halo" cx=${node.x} cy=${node.y} r="8"></circle>` : null}
                <circle class="tree-graph-node__dot" cx=${node.x} cy=${node.y} r=${node.current ? 4.5 : 3.5}></circle>
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
                class=${`tree-entry ${entry.current ? "is-current" : ""} ${entry.active ? "is-active" : "is-abandoned"} ${selected ? "is-selected" : ""}`}
              >
                <div class="tree-entry__graph-cell" aria-hidden="true"></div>
                <button
                  class="tree-entry__summary"
                  disabled=${entry.current || Boolean(previewingID) || navigating}
                  title=${entry.current ? "Current conversation leaf" : "Preview navigation to this node"}
                  onClick=${() => previewEntry(entry.id)}
                >
                  <span class="tree-entry__identity">
                    <span class="tree-kind">${kindLabel(kind)}</span>
                    ${entry.current ? html`<span class="tree-ref tree-ref--head">HEAD</span>` : entry.active ? html`<span class="tree-ref">active</span>` : html`<span class=${`tree-ref tree-lane-color--${graphNode?.lane % 6 || 0}`}>branch</span>`}
                    ${entry.label ? html`<span class="tree-label">${entry.label}</span>` : null}
                  </span>
                  <span class="tree-preview-text">${entry.preview || entry.id}</span>
                </button>
                <div class="tree-entry__right">
                  <time title=${entry.createdAt || ""}>${formatTreeTime(entry.createdAt)}</time>
                  <div class="tree-entry__actions">
                    ${entry.current ? html`<span class="tree-action-unavailable">current</span>` : html`
                      <button disabled=${Boolean(previewingID) || navigating} onClick=${() => previewEntry(entry.id)}>
                        ${previewing ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
                        <span>${previewing ? "Previewing" : selected ? "Previewed" : "Preview"}</span>
                      </button>`}
                    <button disabled=${navigating} onClick=${() => {
                      const label = window.prompt("Node label", entry.label || "");
                      if (label !== null) void controller.labelTreeEntry(sessionID, entry.id, label);
                    }}>Label</button>
                  </div>
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
            <div class="inspector-section-title">Navigation preview</div>
            <div class="navigation-preview__target">${preview.current ? "Already at this node" : "Review what changes before navigating"}</div>
          </div>
        </div>
        ${preview.editorText ? html`<div><span class="field-label">Restored editor text</span><pre>${preview.editorText}</pre></div>` : null}
        ${preview.abandoned?.length ? html`<div>
          <span class="field-label">Work that becomes abandoned${preview.abandonedTruncated ? `, showing ${preview.abandoned.length} of ${preview.abandonedTotal}` : ""}</span>
          <ul>${preview.abandoned.map((item) => html`<li><strong>${item.kind}</strong> ${item.preview || item.id}</li>`)}</ul>
        </div>` : html`<div class="muted">No active work is abandoned.</div>`}
        <label class="stacked-label">Branch summary, optional<textarea rows="4" value=${summary} disabled=${navigating} onInput=${(event) => setSummary(event.currentTarget.value)}></textarea></label>
        <button class="primary navigation-preview__action" disabled=${preview.current || navigating} onClick=${navigateToPreview}>
          ${navigating ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
          <span>${navigating ? "Navigating" : "Navigate here"}</span>
        </button>
      </aside>` : null}
    </div>
  </div>`;
}

function isMessageEntry(entry) {
  if (entry.kind === "user" || entry.kind === "assistant") return true;
  return entry.kind === "assistant_tool_calls" && Boolean(entry.preview);
}

function kindLabel(kind) {
  return String(kind || "node").replace(/^assistant_tool_calls$/, "assistant").replace(/_/g, " ");
}

function formatTreeTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
