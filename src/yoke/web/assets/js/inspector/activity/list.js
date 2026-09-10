import { html, useLayoutEffect, useRef } from "../../../vendor/htm-preact.js";
import { listScrollPosition, loadedScope, nearBottom, restoredScrollTop, selectedOutsideWindow } from "./logic.js";
import { callOutcome, formatDuration, formatTimestamp } from "./presenters.js";
import { compactCallSignature } from "./data.js";

export function ActivityList({ calls, visible, total, cursor, selectedID, detail, preferences, patchPreferences, busy, error, loading, windowChanged, onRefresh, onLoadEarlier, onLatest, onSelect }) {
  const nodeRef = useRef(null);
  const previousPane = useRef(preferences.pane);
  const { search, following } = preferences;
  const filtered = Boolean(search.trim());
  const outsideLabel = loading && selectedID ? "Selected call; history is loading" : selectedOutsideWindow(calls, visible, selectedID);
  const separateCall = calls.find((call) => call.id === selectedID) || detail || { id: selectedID, toolName: "tool", arguments: { raw: "{}" } };
  const latestID = calls.at(-1)?.id;
  const visibleKey = visible.map((call) => call.id).join("\0");

  useLayoutEffect(() => {
    const node = nodeRef.current;
    if (!node?.clientHeight) return;
    const anchor = [...node.querySelectorAll("[data-call-id]")].find((row) => row.dataset.callId === preferences.anchorID);
    if (!following && anchor) {
      node.scrollTop += anchor.getBoundingClientRect().top - node.getBoundingClientRect().top - (preferences.anchorOffset || 0);
    } else {
      node.scrollTop = restoredScrollTop(node, preferences.scrollTop, following && !filtered);
    }
  }, [visibleKey, preferences.pane, following, search]);

  useLayoutEffect(() => {
    if (previousPane.current === "detail" && preferences.pane === "list") {
      nodeRef.current?.closest(".activity-sidebar")?.querySelector('.activity-row[aria-current="true"]')?.focus({ preventScroll: true });
    }
    previousPane.current = preferences.pane;
  }, [preferences.pane]);

  const setSearch = (value) => patchPreferences({ search: value, following: false, scrollTop: 0, anchorID: null });
  return html`<aside class="activity-sidebar" aria-label="Tool activity list">
    <div class="activity-sidebar__header">
      <div class="activity-sidebar__topline">
        <p><span class="activity-scope">${loading ? "Loading tool history…" : loadedScope(calls.length, total, visible.length, filtered)}</span><span class="activity-direction">Oldest to newest</span></p>
        <div><button disabled=${Boolean(busy)} onClick=${onRefresh}>${busy === "refresh" ? "Refreshing…" : "Refresh"}</button><button disabled=${Boolean(busy)} onClick=${onLatest}>${preferences.newCalls > 0 ? `Latest · ${preferences.newCalls} new` : "Latest"}</button></div>
      </div>
      <label class="activity-search"><span class="visually-hidden">Search loaded calls</span><input type="search" value=${search} placeholder="Search tool calls" onInput=${(event) => setSearch(event.currentTarget.value)} />${search ? html`<button onClick=${() => setSearch("")}>Clear</button>` : null}</label>
      ${error ? html`<div class="activity-request-error" role="alert">${error}<button disabled=${Boolean(busy)} onClick=${onRefresh}>Retry</button></div>` : null}
      ${windowChanged ? html`<p class="activity-note">History changed. Selection preserved.</p>` : null}
      ${outsideLabel ? html`<div class="activity-selected-outside"><p>${outsideLabel}${loading ? "." : outsideLabel.includes("outside") ? cursor?.next != null ? ". Load earlier to reach it." : "." : ". Clear search to reveal it."}</p><${ActivityRow} call=${separateCall} selected=${true} onSelect=${onSelect} /></div>` : null}
    </div>
    <div class="activity-list" ref=${nodeRef} tabIndex="0" aria-label="Calls in start order" onScroll=${(event) => {
      const node = event.currentTarget;
      patchPreferences({ ...listScrollPosition(node), following: following && !filtered && nearBottom(node) });
    }}>
      <div class="activity-earlier">
        ${cursor?.next != null ? html`<button disabled=${Boolean(busy)} onClick=${() => {
          if (nodeRef.current) patchPreferences({ ...listScrollPosition(nodeRef.current), following: false });
          return onLoadEarlier();
        }}>${busy === "earlier" ? "Loading earlier…" : "Load earlier"}</button>` : html`<span>${Number.isFinite(total) && calls.length >= total ? "Start of history" : "No earlier page available"}</span>`}
      </div>
      <div class="activity-column-labels" aria-hidden="true"><span>Call</span><span>Started</span><span>Status</span></div>
      ${visible.map((call) => html`<${ActivityRow} key=${call.id} call=${call} selected=${selectedID === call.id} latest=${call.id === latestID} onSelect=${onSelect} />`)}
      ${!visible.length ? html`<div class="activity-empty"><strong>${loading ? "Loading tool activity…" : error && !calls.length ? "Tool history unavailable" : calls.length ? "No matching calls" : "No tool calls yet"}</strong><p>${loading ? "The selected detail can remain open while history loads." : error && !calls.length ? "Retry to load tool history." : calls.length ? "Search covers the loaded history." : "Tool calls will appear here when this session uses a tool."}</p>${search ? html`<button onClick=${() => setSearch("")}>Clear search</button>` : null}</div>` : null}
    </div>
  </aside>`;
}

function ActivityRow({ call, selected, latest, onSelect }) {
  const outcome = callOutcome(call);
  const signature = compactCallSignature(call);
  return html`<button class=${`activity-row ${selected ? "is-selected" : ""}`} data-call-id=${call.id} aria-current=${selected ? "true" : null} onClick=${() => onSelect(call.id)} title=${signature}>
    <span class="activity-row__call"><code>${signature}</code>${latest ? html`<span>latest loaded</span>` : null}</span>
    <span class="activity-row__time"><time dateTime=${call.time?.started || null}>${formatTimestamp(call.time?.started)}</time><span>${formatDuration(call.time?.durationMs)}</span></span>
    <span class=${`activity-row__status activity-status--${outcome.tone}`}>${outcome.label}</span>
  </button>`;
}
