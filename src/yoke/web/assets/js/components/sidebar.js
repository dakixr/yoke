import { html, useEffect, useMemo, useState } from "../../vendor/htm-preact.js";
import { workingDuration, shortAge } from "../lib/duration.js";
import { currentRoute, draftPath, navigate } from "../router/router.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { SessionContextMenu } from "./session-context-menu.js";
import { connectionStatusDescriptor, hasPendingQueue, sessionStatusDescriptor } from "./sidebar-status.js";

export function Sidebar({ peeking = false, onPointerEnter = null, onPointerLeave = null, onTransientClose = null }) {
  const open = useStore((state) => state.ui.sidebarOpen);
  const sessions = useStore((state) => state.sessions);
  const order = useStore((state) => state.sessionOrder);
  const archivedOrder = useStore((state) => state.archivedOrder);
  const archivedTotal = useStore((state) => state.archivedTotal);
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
  const capabilities = useStore((state) => state.capabilities);
  const connection = useStore((state) => state.connection);
  const [settledOpen, setSettledOpen] = useState(false);
  const [projectScope, setProjectScope] = useState("");
  const [scopedSettledTotal, setScopedSettledTotal] = useState(null);
  const [contextMenu, setContextMenu] = useState(null);
  const route = currentRoute();
  const selectedDraftID = route.name === "new" ? route.draftID : null;
  const connectionStatus = connectionStatusDescriptor(connection);
  const connected = connection.current;

  const meaningfulDrafts = useMemo(
    () => Object.values(drafts)
      .filter((draft) => (draft.text || "").trim() || draft.attachments?.length)
      .sort((left, right) => String(right.updatedAt || "").localeCompare(String(left.updatedAt || ""))),
    [drafts],
  );
  const projects = useMemo(
    () => projectOptions(order.map((id) => sessions[id]).filter(Boolean), meaningfulDrafts, locations),
    [order, sessions, meaningfulDrafts, locations],
  );
  useEffect(() => {
    if (projectScope && !projects.some((project) => project.directory === projectScope)) setProjectScope("");
  }, [projectScope, projects]);
  const matchesScope = (item) => !projectScope || item?.location?.directory === projectScope || item?.location === projectScope;
  const current = order.map((id) => sessions[id]).filter((item) => item && (matchesScope(item) || item.id === selectedID));
  const pinned = current.filter((item) => item.pinned);
  const inbox = current.filter((item) => !item.pinned);
  const scopedDrafts = meaningfulDrafts.filter((draft) => matchesScope(draft));
  const scopedArchived = archivedOrder.map((id) => sessions[id]).filter((session) => session && matchesScope(session));
  const settledTotal = projectScope ? (scopedSettledTotal ?? scopedArchived.length) : archivedTotal;
  useEffect(() => {
    if (!projectScope) {
      setScopedSettledTotal(null);
      return undefined;
    }
    let cancelled = false;
    setScopedSettledTotal(null);
    void controller.countSessions({ directory: projectScope, archived: true })
      .then((total) => {
        if (!cancelled) setScopedSettledTotal(total);
      })
      .catch(() => {
        if (!cancelled) setScopedSettledTotal(scopedArchived.length);
      });
    return () => { cancelled = true; };
  }, [projectScope, archivedTotal]);
  const visibleSearch = search.trim()
    ? searchResults.map((id) => sessions[id]).filter((item) => item && matchesScope(item))
    : null;
  const openSessionMenu = (event, session) => {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    setContextMenu({
      sessionID: session.id,
      x: Number.isFinite(event.clientX) && event.clientX > 0 ? event.clientX : rect.left + 24,
      y: Number.isFinite(event.clientY) && event.clientY > 0 ? event.clientY : rect.top + 24,
    });
  };

  return html`
    <aside
      class=${`sidebar ${open ? "is-open" : ""} ${peeking ? "is-peeking" : ""}`}
      aria-label="Sessions"
      onPointerEnter=${onPointerEnter}
      onPointerLeave=${onPointerLeave}
    >
      <div class="sidebar__top">
        <div class="brand-row">
          <button class="icon-button mobile-only" aria-label="Close sessions" onClick=${() => peeking ? onTransientClose?.() : controller.toggleSidebar()}>×</button>
          <div class="brand">Yoke</div>
          <span class="brand-subtitle">Sessions</span>
          <span class=${`connection-dot is-${connectionStatus.kind}`} aria-label=${connectionStatus.label} title=${connectionStatus.label}></span>
        </div>
        <div class="sidebar-inbox-toolbar">
          <label class="sidebar-search">
            <span class="sidebar-search__icon" aria-hidden="true">⌕</span>
            <input
              type="search"
              value=${search}
              placeholder="Search"
              aria-label="Search sessions"
              onInput=${(event) => controller.searchSessions(event.currentTarget.value)}
            />
            ${searching ? html`<span class="muted tiny">…</span>` : null}
          </label>
          <button class="new-session-button" aria-label="New session" title="New session" onClick=${() => controller.createDraft()}>＋</button>
        </div>
        <label class="sidebar-project-filter">
          <span aria-hidden="true">▱</span>
          <select aria-label="Filter sessions by project" value=${projectScope} onChange=${(event) => setProjectScope(event.currentTarget.value)}>
            <option value="">All projects</option>
            ${projects.map((project) => html`<option value=${project.directory}>${project.name}</option>`)}
          </select>
        </label>
      </div>
      <div class="sidebar__scroll">
        ${visibleSearch ? html`
          <div class="inbox-list" role="list" aria-label="Search results">
            ${visibleSearch.length ? visibleSearch.map((session) => html`
              <${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} locations=${locations} settleSupported=${capabilities?.features?.sessionArchive} connected=${connected} onOpenMenu=${openSessionMenu} />
            `) : html`<div class="sidebar-empty">No sessions found</div>`}
          </div>
        ` : html`
          <div class="inbox-list" role="list">
            ${scopedDrafts.map((draft) => html`<${DraftRow} key=${draft.id} draft=${draft} location=${locations[draft.location]} selected=${selectedDraftID === draft.id} />`)}
            ${pinned.map((session) => html`
              <${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} locations=${locations} pinned outsideScope=${Boolean(projectScope && !matchesScope(session))} settleSupported=${capabilities?.features?.sessionArchive} connected=${connected} onOpenMenu=${openSessionMenu} />
            `)}
            ${pinned.length && inbox.length ? html`<div class="pinned-divider" aria-hidden="true"></div>` : null}
            ${inbox.map((session) => html`
              <${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} locations=${locations} outsideScope=${Boolean(projectScope && !matchesScope(session))} settleSupported=${capabilities?.features?.sessionArchive} connected=${connected} onOpenMenu=${openSessionMenu} />
            `)}
            ${sessionCursor ? html`<button class="sidebar-more" onClick=${() => controller.loadMoreSessions(false)}>＋ Load older sessions</button>` : null}
            ${!scopedDrafts.length && !pinned.length && !inbox.length ? html`<div class="sidebar-empty">No sessions yet</div>` : null}
          </div>
          ${settledTotal > 0 ? html`
            <section class="settled-shelf">
              <button class="section-toggle" aria-expanded=${settledOpen} onClick=${() => setSettledOpen(!settledOpen)}>
                <span>Settled (${settledTotal})</span>
                <span class="section-toggle__line" aria-hidden="true"></span>
                <span class=${`section-toggle__chevron ${settledOpen ? "is-open" : ""}`} aria-hidden="true">⌄</span>
              </button>
              ${settledOpen ? scopedArchived.map((session) => html`
                <${SessionRow} key=${session.id} session=${session} active=${active[session.id]} attention=${attention[session.id]} selected=${selectedID === session.id} done=${done[session.id]} locations=${locations} settled onOpenMenu=${openSessionMenu} />
              `) : null}
              ${settledOpen && archivedCursor ? html`<button class="sidebar-more" onClick=${() => controller.loadMoreSessions(true)}>＋ Show more settled</button>` : null}
            </section>
          ` : null}
        `}
      </div>
      <div class="sidebar__footer">
        <button class="quiet-button" onClick=${() => controller.togglePalette(true)}>Command palette <kbd>⌘K</kbd></button>
      </div>
      ${contextMenu && sessions[contextMenu.sessionID] ? html`
        <${SessionContextMenu}
          session=${sessions[contextMenu.sessionID]}
          location=${locations[sessions[contextMenu.sessionID].location?.directory || ""]}
          runtime=${active[contextMenu.sessionID]}
          capabilities=${capabilities}
          position=${contextMenu}
          onClose=${() => setContextMenu(null)}
        />
      ` : null}
      <${SidebarResizeHandle} />
    </aside>
  `;
}

function SessionRow({ session, active, attention, selected, done, locations, pinned = false, settled = false, outsideScope = false, settleSupported = false, connected = true, onOpenMenu }) {
  const directory = session.location?.directory || "";
  const location = locations[directory];
  const projectName = location?.name || compactPath(directory);
  const branch = location?.git?.branch || compactPath(directory);
  const model = session.selection?.model || session.selection?.provider || "";
  const age = shortAge(
    settled
      ? session.archivedAt
      : session.time?.lastUserMessage || session.time?.created || session.time?.updated,
  );
  const attentionCount = (attention?.permissions || 0) + (attention?.questions || 0);
  const working = active?.state === "running" && attentionCount === 0;
  const busy = active?.state && active.state !== "idle" && active.state !== "error";
  const quickSettle = Boolean(settleSupported && !settled && !busy && !hasPendingQueue(session.queue));
  const menuKeys = (event) => {
    if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) onOpenMenu?.(event, session);
  };
  if (settled) {
    const status = sessionStatusDescriptor({ runtime: active, attention, done, queue: session.queue, age });
    return html`
      <button class=${`session-row session-row--slim is-settled ${selected ? "is-selected" : ""}`} aria-current=${selected ? "page" : undefined} onClick=${() => controller.selectSession(session.id)} onContextMenu=${(event) => onOpenMenu?.(event, session)} onKeyDown=${menuKeys}>
        <span class="session-project-glyph" aria-hidden="true">▱</span>
        <span class="session-row__title">${session.title || session.id}</span>
        ${session.pinned ? html`<span class="session-pin" aria-label="Pinned" title="Pinned">PIN</span>` : null}
        <span class=${`session-row__meta status--${status.kind}`}>${status.label}</span>
      </button>
    `;
  }
  return html`
    <div class=${`session-card-wrap ${quickSettle ? "has-quick-action" : ""}`} role="listitem">
      <button
        class=${`session-row session-card ${selected ? "is-selected" : ""} ${working ? "is-working" : ""} ${outsideScope ? "is-outside-scope" : ""}`}
        aria-current=${selected ? "page" : undefined}
        onClick=${() => controller.selectSession(session.id)}
        onContextMenu=${(event) => onOpenMenu?.(event, session)}
        onKeyDown=${menuKeys}
      >
        <span class="session-card__top">
          <span class="session-project-glyph" aria-hidden="true">▱</span>
          <span class="session-card__project" title=${directory}>${projectName}</span>
          ${(pinned || session.pinned) ? html`<span class="session-pin" aria-label="Pinned" title="Pinned">PIN</span>` : null}
          <span class="session-card__status-slot"><${SessionStatus} runtime=${active} attention=${attention} done=${done} queue=${session.queue} age=${age} /></span>
        </span>
        <span class="session-row__title">${session.title || session.id}</span>
        <span class="session-card__bottom">
          <span class="session-card__branch" title=${directory}>${outsideScope ? `Current · ${branch}` : branch}</span>
          ${model ? html`<span class="session-card__model" title=${model}>${model}</span>` : null}
        </span>
      </button>
      ${quickSettle ? html`
        <button
          class="session-quick-action"
          type="button"
          aria-label=${`Settle ${session.title || "session"}`}
          disabled=${!connected}
          onClick=${(event) => {
            event.stopPropagation();
            controller.patchSession(session.id, { archived: true }).catch((error) => controller.notice(error?.message || String(error)));
          }}
        >
          <svg aria-hidden="true" viewBox="0 0 16 16" width="13" height="13"><path d="m3 8.2 3 3L13 4.7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"></path></svg>
          <span>Settle</span>
        </button>
      ` : null}
    </div>
  `;
}

function SessionStatus({ runtime, attention, done, queue, age }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (runtime?.state !== "running") return;
    const id = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(id);
  }, [runtime?.state, runtime?.startedAt]);
  const status = sessionStatusDescriptor({ runtime, attention, done, queue, age });
  if (status.kind === "working") {
    return html`<span class="status status--working"><span class="working-glyph" aria-hidden="true"></span><span role="status">Working</span><span class="working-duration" aria-hidden="true">${workingDuration(runtime.startedAt).replace("Working ", "")}</span></span>`;
  }
  return html`<span class=${`status status--${status.kind}`}>${status.label}</span>`;
}

function DraftRow({ draft, location, selected }) {
  const preview = (draft.text || "Untitled draft").trim().replace(/\s+/g, " ").slice(0, 72);
  const projectName = location?.name || compactPath(draft.location);
  return html`<button class=${`session-row session-card draft-row ${selected ? "is-selected" : ""}`} aria-current=${selected ? "page" : undefined} onClick=${() => navigate(draftPath(draft.id))}>
    <span class="session-card__top">
      <span class="session-project-glyph" aria-hidden="true">▱</span>
      <span class="session-card__project">${projectName}</span>
      <span class="status status--draft">Draft</span>
    </span>
    <span class="session-row__title">${preview}</span>
    <span class="session-card__bottom">
      <span class="session-card__branch">${draft.attachments?.length ? `${draft.attachments.length} attachment${draft.attachments.length === 1 ? "" : "s"}` : compactPath(draft.location)}</span>
      <span class="session-row__meta">${shortAge(draft.updatedAt)}</span>
    </span>
  </button>`;
}

function SidebarResizeHandle() {
  const onPointerDown = (event) => {
    if (window.innerWidth <= 900) return;
    const startX = event.clientX;
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-width")) || 286;
    const move = (moveEvent) => {
      const width = Math.max(250, Math.min(430, current + moveEvent.clientX - startX));
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

function projectOptions(sessions, drafts, locations) {
  const directories = new Set([
    ...sessions.map((session) => session.location?.directory).filter(Boolean),
    ...drafts.map((draft) => draft.location).filter(Boolean),
  ]);
  return [...directories]
    .map((directory) => ({ directory, name: locations[directory]?.name || compactPath(directory) }))
    .sort((left, right) => left.name.localeCompare(right.name) || left.directory.localeCompare(right.directory));
}

function compactPath(path) {
  if (!path) return "Unknown project";
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) || path;
}
