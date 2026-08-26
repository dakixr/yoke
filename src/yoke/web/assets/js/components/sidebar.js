import { html, useEffect, useMemo, useState } from "../../vendor/htm-preact.js";
import { workingDuration, shortAge } from "../lib/duration.js";
import { draftPath, navigate } from "../router/router.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";

export function Sidebar() {
  const open = useStore((state) => state.ui.sidebarOpen);
  const sessions = useStore((state) => state.sessions);
  const order = useStore((state) => state.sessionOrder);
  const archivedOrder = useStore((state) => state.archivedOrder);
  const archivedCursor = useStore((state) => state.archivedCursor);
  const sessionCursor = useStore((state) => state.sessionsCursor);
  const active = useStore((state) => state.active);
  const attention = useStore((state) => state.attention);
  const selectedID = useStore((state) => state.ui.selectedSessionID);
  const done = useStore((state) => state.ui.doneUnreviewed);
  const drafts = useStore((state) => state.drafts);
  const locations = useStore((state) => state.locations);
  const search = useStore((state) => state.ui.search);
  const searchResults = useStore((state) => state.ui.searchResults);
  const searching = useStore((state) => state.ui.searching);
  const [settledOpen, setSettledOpen] = useState(false);

  const meaningfulDrafts = useMemo(
    () => Object.values(drafts).filter((draft) => (draft.text || "").trim() || draft.attachments?.length),
    [drafts],
  );
  const pinned = order.map((id) => sessions[id]).filter((item) => item?.pinned);
  const unpinned = order.map((id) => sessions[id]).filter((item) => item && !item.pinned);
  const groups = useMemo(() => groupByLocation(unpinned, locations), [unpinned, locations]);
  const visibleSearch = search.trim() ? searchResults.map((id) => sessions[id]).filter(Boolean) : null;

  return html`
    <aside class=${`sidebar ${open ? "is-open" : ""}`} aria-label="Sessions">
      <div class="sidebar__top">
        <div class="brand-row">
          <button class="icon-button mobile-only" aria-label="Close sessions" onClick=${() => controller.toggleSidebar()}>×</button>
          <div class="brand">Yoke</div>
          <span class="connection-dot" aria-hidden="true"></span>
        </div>
        <button class="new-session-button" onClick=${() => controller.createDraft()}>＋ New session <kbd>⌘N</kbd></button>
        <label class="sidebar-search">
          <span aria-hidden="true">⌕</span>
          <input
            type="search"
            value=${search}
            placeholder="Search sessions"
            aria-label="Search sessions"
            onInput=${(event) => controller.searchSessions(event.currentTarget.value)}
          />
          ${searching ? html`<span class="muted tiny">…</span>` : null}
        </label>
      </div>
      <div class="sidebar__scroll">
        ${visibleSearch ? html`
          <${SidebarSection} title="Search" count=${visibleSearch.length}>
            ${visibleSearch.map((session) => html`<${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} />`)}
          <//>
        ` : html`
          ${meaningfulDrafts.length ? html`
            <${SidebarSection} title="Drafts" count=${meaningfulDrafts.length}>
              ${meaningfulDrafts.map((draft) => html`<${DraftRow} key=${draft.id} draft=${draft} location=${locations[draft.location]} />`)}
            <//>
          ` : null}
          ${pinned.length ? html`
            <${SidebarSection} title="Pinned" count=${pinned.length}>
              ${pinned.map((session) => html`<${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} pinned />`)}
            <//>
          ` : null}
          ${groups.map((group) => html`
            <${SidebarSection} key=${group.directory} title=${group.name} subtitle=${group.git?.branch || compactPath(group.directory)}>
              ${group.sessions.map((session) => html`<${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} />`)}
            <//>
          `)}
          ${sessionCursor ? html`<button class="sidebar-more" onClick=${() => controller.loadMoreSessions(false)}>Load older sessions</button>` : null}
          ${archivedOrder.length ? html`
            <section class="sidebar-section settled-section">
              <button class="section-toggle" aria-expanded=${settledOpen} onClick=${() => setSettledOpen(!settledOpen)}>
                <span>${settledOpen ? "▾" : "▸"} Settled</span><span>${archivedOrder.length}</span>
              </button>
              ${settledOpen ? archivedOrder.map((id) => sessions[id]).filter(Boolean).map((session) => html`
                <${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} settled />
              `) : null}
              ${settledOpen && archivedCursor ? html`<button class="sidebar-more" onClick=${() => controller.loadMoreSessions(true)}>Show more settled</button>` : null}
            </section>
          ` : null}
        `}
      </div>
      <div class="sidebar__footer">
        <button class="quiet-button" onClick=${() => controller.togglePalette(true)}>Command palette <kbd>⌘K</kbd></button>
      </div>
      <${SidebarResizeHandle} />
    </aside>
  `;
}

function SidebarSection({ title, subtitle = null, count = null, children }) {
  return html`<section class="sidebar-section">
    <div class="sidebar-section__heading"><span>${title}</span>${count !== null ? html`<span>${count}</span>` : null}</div>
    ${subtitle ? html`<div class="sidebar-section__subtitle" title=${subtitle}>${subtitle}</div>` : null}
    <div class="sidebar-section__rows">${children}</div>
  </section>`;
}

function SessionRow({ session, active, attention, selected, done, settled = false }) {
  const age = shortAge(settled ? session.archivedAt : session.time?.updated);
  const showAge = settled || (!active?.state && !attention?.permissions && !attention?.questions && !done && age !== "now");
  return html`
    <button
      class=${`session-row ${selected ? "is-selected" : ""} ${settled ? "is-settled" : ""}`}
      aria-current=${selected ? "page" : undefined}
      onClick=${() => controller.selectSession(session.id)}
    >
      <span class="session-row__main">
        <span class="session-row__title">${session.title || session.id}</span>
        <${SessionStatus} runtime=${active} attention=${attention} done=${done} queue=${session.queue} settled=${settled} />
      </span>
      <span class="session-row__meta">
        ${showAge ? html`<span>${age}</span>` : null}
      </span>
    </button>
  `;
}

function SessionStatus({ runtime, attention, done, queue, settled }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (runtime?.state !== "running") return;
    const id = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(id);
  }, [runtime?.state, runtime?.startedAt]);
  if (settled) return html`<span class="status status--quiet">Settled</span>`;
  const attentionCount = (attention?.permissions || 0) + (attention?.questions || 0);
  if (attentionCount) return html`<span class="status status--attention">${attentionCount === 1 ? (attention?.permissions ? "Permission required" : "Question waiting") : `${attentionCount} actions required`}</span>`;
  if (runtime?.state === "waiting_input") return html`<span class="status status--attention">Waiting for you</span>`;
  if (runtime?.state === "error") return html`<span class="status status--error">Error</span>`;
  if (done) return html`<span class="status status--done">Done</span>`;
  if (runtime?.state === "stopping") return html`<span class="status status--quiet">Stopping</span>`;
  if (runtime?.state === "running") return html`<span class="status status--working">${workingDuration(runtime.startedAt)}</span>`;
  if (queue?.steering && queue?.queued) return html`<span class="status status--quiet">${queue.steering} steer · ${queue.queued} queued</span>`;
  if (queue?.steering) return html`<span class="status status--quiet">${queue.steering} steer pending</span>`;
  if (queue?.queued) return html`<span class="status status--quiet">${queue.queued} queued</span>`;
  return html`<span class="status status--quiet">Idle</span>`;
}

function DraftRow({ draft, location }) {
  const preview = (draft.text || "Untitled draft").trim().replace(/\s+/g, " ").slice(0, 64);
  return html`<button class="session-row draft-row" onClick=${() => navigate(draftPath(draft.id))}>
    <span class="session-row__main"><span class="session-row__title">${preview}</span><span class="status status--draft">Draft · ${location?.name || compactPath(draft.location)}</span></span>
    <span class="session-row__meta">${shortAge(draft.updatedAt)}</span>
  </button>`;
}

function SidebarResizeHandle() {
  const onPointerDown = (event) => {
    if (window.innerWidth <= 900) return;
    const startX = event.clientX;
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-width")) || 286;
    const move = (moveEvent) => {
      const width = Math.max(230, Math.min(420, current + moveEvent.clientX - startX));
      document.documentElement.style.setProperty("--sidebar-width", `${width}px`);
      localStorage.setItem("yoke.web.sidebarWidth", String(width));
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  };
  return html`<div class="sidebar-resize" role="separator" aria-orientation="vertical" onPointerDown=${onPointerDown}></div>`;
}

function groupByLocation(sessions, locations) {
  const map = new Map();
  for (const session of sessions) {
    const directory = session.location?.directory || "";
    if (!map.has(directory)) map.set(directory, []);
    map.get(directory).push(session);
  }
  return [...map.entries()]
    .map(([directory, items]) => ({
      directory,
      name: locations[directory]?.name || compactPath(directory),
      git: locations[directory]?.git || null,
      sessions: items,
    }))
    .sort((left, right) => left.name.localeCompare(right.name) || left.directory.localeCompare(right.directory));
}

function compactPath(path) {
  if (!path) return "Unknown location";
  const parts = path.split("/").filter(Boolean);
  return parts.at(-1) || path;
}
