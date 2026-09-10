import { html } from "../../../vendor/htm-preact.js";
import { TREE_GRAPH_ROW_HEIGHT } from "../tree-graph.js";
import { entryText, formatTreeTime, kindLabel } from "./model.js";

export function TreeHistory({ graph, rows, latestID, selectedID, focusID, matches, pending, moving, historyRef, buttonRefs, onSelect, onFocus, onKeyDown, onScroll }) {
  const matchIDs = new Set(matches);
  return html`<section class="tree-history" ref=${historyRef} aria-label="Conversation graph" onScroll=${onScroll}>
    <div class="tree-history__columns" style=${`--tree-graph-width:${graph.graphWidth}px`}><span>Graph</span><span>Conversation</span><span>When</span></div>
    ${rows.length ? html`<div class="tree-graph-rows" style=${`--tree-graph-width:${graph.graphWidth}px;--tree-row-height:${TREE_GRAPH_ROW_HEIGHT}px`}>
      <svg class="tree-graph-canvas" width=${graph.graphWidth} height=${graph.height} viewBox=${`0 0 ${graph.graphWidth} ${graph.height}`} aria-hidden="true">
        ${graph.nodes.filter((node) => node.externalParent).map((node) => html`<path key=${`external:${node.id}`} class="tree-graph-edge is-external" d=${`M ${node.x} ${Math.max(0, node.y - TREE_GRAPH_ROW_HEIGHT / 2)} L ${node.x} ${node.y}`} />`)}
        ${graph.edges.map((edge) => html`<path key=${`${edge.parentID}:${edge.childID}`} class=${`tree-graph-edge ${edge.active ? "is-active" : ""}`} d=${edge.path} />`)}
        ${graph.nodes.map((node) => html`<g key=${node.id} class=${`tree-graph-node ${node.current ? "is-current" : ""} ${node.active ? "is-active" : ""} ${selectedID === node.id ? "is-target" : ""}`}>
          ${node.current ? html`<circle class="tree-graph-node__halo" cx=${node.x} cy=${node.y} r="9" />` : null}
          ${selectedID === node.id ? html`<circle class="tree-graph-node__target" cx=${node.x} cy=${node.y} r="7" />` : null}
          <circle class="tree-graph-node__dot" cx=${node.x} cy=${node.y} r=${node.current ? 4.8 : 3.7} />
        </g>`)}
      </svg>
      <div class="tree-list" role="list" aria-label="Conversation tree nodes">
        ${rows.map((entry) => html`<div key=${entry.id} role="listitem" class=${`tree-entry ${entry.current ? "is-current" : ""} ${selectedID === entry.id ? "is-selected" : ""} ${matchIDs.has(entry.id) ? "is-match" : ""}`} onClick=${(event) => {
          if (!event.target.closest("button")) onSelect(entry.id);
        }}>
          <div class="tree-entry__graph-cell" aria-hidden="true"></div>
          <button ref=${(node) => node ? buttonRefs.current.set(entry.id, node) : buttonRefs.current.delete(entry.id)} class="tree-entry__summary"
            disabled=${moving} tabindex=${focusID === entry.id ? 0 : -1} aria-current=${entry.current ? "true" : null}
            aria-pressed=${selectedID === entry.id} aria-busy=${pending && selectedID === entry.id ? "true" : null}
            onFocus=${() => onFocus(entry.id)} onClick=${() => onSelect(entry.id)} onKeyDown=${(event) => onKeyDown(event, entry)}>
            <span class="tree-entry__identity">
              ${entry.current ? html`<strong class="tree-ref--head">Current HEAD</strong>` : null}
              <span>${kindLabel(entry.kind)}</span>
              ${latestID === entry.id ? html`<span class="tree-ref--latest">Latest</span>` : null}
              ${entry.childCount > 1 ? html`<span title="Fork paths including technical nodes">Fork · ${entry.childCount}</span>` : null}
              ${entry.label ? html`<strong class="tree-label">${entry.label}</strong>` : null}
              ${entry.technicalAnchor ? html`<span class="tree-anchor-note">Technical anchor</span>` : null}
            </span>
            <span class="tree-preview-text">${entryText(entry)}</span>
          </button>
          <time class="tree-entry__time" datetime=${entry.createdAt} title=${formatTreeTime(entry.createdAt, { details: true })}>${formatTreeTime(entry.createdAt)}</time>
        </div>`)}
      </div>
    </div>` : html`<p class="tree-empty">No messages in this loaded window. Try All nodes or load older history.</p>`}
  </section>`;
}

export function keepTreeEntryVisible(scroller, button) {
  if (!scroller || !button) return;
  const row = button.closest(".tree-entry") || button;
  const scrollerRect = scroller.getBoundingClientRect();
  const columnsBottom = scroller.querySelector(".tree-history__columns")?.getBoundingClientRect().bottom || scrollerRect.top;
  const rowRect = row.getBoundingClientRect();
  const visibleTop = Math.max(scrollerRect.top, columnsBottom);
  if (rowRect.top < visibleTop) scroller.scrollTop -= visibleTop - rowRect.top;
  else if (rowRect.bottom > scrollerRect.bottom) scroller.scrollTop += rowRect.bottom - scrollerRect.bottom;
}
