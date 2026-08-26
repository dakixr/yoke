import { html } from "../../vendor/htm-preact.js";
import { currentRoute } from "../router/router.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { DraftComposer, SessionComposer } from "./composer.js";
import { HumanInput } from "./human-input.js";
import { QueueEditor } from "./queue.js";
import { Timeline } from "./timeline.js";

export function MainView() {
  const newSession = useStore((state) => state.ui.newSession);
  if (newSession) return html`<${NewSessionView} />`;
  return html`<${SessionView} />`;
}

function NewSessionView() {
  const route = currentRoute();
  const draftID = route.name === "new" ? route.draftID : null;
  const drafts = useStore((state) => state.drafts);
  if (!draftID) return html`<div class="content-loading">Preparing draft…</div>`;
  return html`<main class="main-view new-session-view">
    <div class="mobile-titlebar"><button class="icon-button" aria-label="Open sessions" onClick=${() => controller.toggleSidebar()}>☰</button><span>New session</span></div>
    <${DraftComposer} draftID=${draftID} draft=${drafts[draftID]} />
  </main>`;
}

function SessionView() {
  const sessionID = useStore((state) => state.ui.selectedSessionID);
  const session = useStore((state) => sessionID ? state.sessions[sessionID] : null);
  const runtime = useStore((state) => sessionID ? state.active[sessionID] : null);
  const data = useStore((state) => sessionID ? state.sessionData[sessionID] : null);
  if (!sessionID) return html`<main class="main-view"><div class="content-loading">Choose a session.</div></main>`;
  if (!session || data?.loading) return html`<main class="main-view"><div class="content-loading">Loading session…</div></main>`;
  if (data?.loadError) return html`<main class="main-view"><div class="content-error">${data.loadError}</div></main>`;
  const attentionCount = (data?.permissions?.length || 0) + (data?.questions?.length || 0);
  return html`<main class="main-view session-view">
    <${SessionHeader} session=${session} runtime=${runtime} attentionCount=${(data?.permissions?.length || 0) + (data?.questions?.length || 0)} />
    <${Timeline} sessionID=${sessionID} data=${data} />
    <div class="session-bottom">
      <div class="session-bottom__inner">
        <${HumanInput} sessionID=${sessionID} permissions=${data?.permissions || []} questions=${data?.questions || []} />
        <${QueueEditor} sessionID=${sessionID} queue=${data?.queue} />
        <${SessionComposer} sessionID=${sessionID} session=${session} runtime=${runtime} data=${data} attentionCount=${attentionCount} />
      </div>
    </div>
  </main>`;
}

function SessionHeader({ session, runtime, attentionCount = 0 }) {
  const capabilities = useStore((state) => state.capabilities);
  const connected = useStore((state) => state.connection.current);
  const inspector = useStore((state) => state.ui.inspector);
  const location = useStore((state) => state.locations[session.location.directory]);
  const busy = runtime?.state && runtime.state !== "idle" && runtime.state !== "error";
  const rename = async () => {
    const title = window.prompt("Session title", session.title || "");
    if (title === null) return;
    try { await controller.patchSession(session.id, { title }); }
    catch (error) { controller.notice(error?.message || String(error)); }
  };
  const archive = async () => {
    try { await controller.patchSession(session.id, { archived: !session.archivedAt }); }
    catch (error) { controller.notice(error?.message || String(error)); }
  };
  return html`<header class="session-header">
    <div class="session-header__identity">
      <button class="icon-button mobile-only" aria-label="Open sessions" onClick=${() => controller.toggleSidebar()}>☰</button>
      <div class="session-header__text">
        <div class="session-header__title-row"><h1 title=${session.title || session.id}>${session.title || session.id}</h1>${session.pinned ? html`<span class="pin-mark" title="Pinned">◆</span>` : null}${attentionCount ? html`<span class="header-attention" role="status">${attentionCount} ${attentionCount === 1 ? "action" : "actions"} required</span>` : null}</div>
        <div class="session-header__location" title=${session.location.directory}>${location?.name || lastPath(session.location.directory)}${location?.git?.branch ? ` · ${location.git.branch}` : ""}</div>
      </div>
    </div>
    <div class="session-header__actions">
      <button class="header-action desktop-session-action" disabled=${!connected} onClick=${rename}>Rename</button>
      <button class="header-action desktop-session-action" disabled=${!connected} onClick=${() => controller.patchSession(session.id, { pinned: !session.pinned }).catch((error) => controller.notice(error?.message || String(error)))}>${session.pinned ? "Unpin" : "Pin"}</button>
      <details class="inspect-menu">
        <summary class=${`header-action ${inspector ? "is-active" : ""}`}>${inspector ? `Inspect · ${inspectorLabel(inspector.mode)}` : "Inspect"} ▾</summary>
        <div class="inspect-menu__popup">
          ${capabilities?.features?.sessionTree ? html`<button onClick=${() => closeDetailsAnd(() => controller.openInspector("tree"))}>Tree</button>` : null}
          ${capabilities?.features?.toolInspector ? html`<button onClick=${() => closeDetailsAnd(() => controller.openInspector("tool"))}>Tool activity</button>` : null}
          ${capabilities?.features?.processInspector ? html`<button onClick=${() => closeDetailsAnd(() => controller.openInspector("process"))}>Processes</button>` : null}
          <button onClick=${() => closeDetailsAnd(() => controller.openInspector("tools"))}>Tools</button>
          ${capabilities?.features?.skills ? html`<button onClick=${() => closeDetailsAnd(() => controller.openInspector("skills"))}>Skills</button>` : null}
          ${capabilities?.features?.mcp ? html`<button onClick=${() => closeDetailsAnd(() => controller.openInspector("mcp"))}>MCP</button>` : null}
          <button onClick=${() => closeDetailsAnd(() => controller.openInspector("context"))}>Session info</button>
        </div>
      </details>
      <button class="header-action desktop-session-action" disabled=${!connected || Boolean(busy)} onClick=${() => controller.compact(session.id).catch((error) => controller.notice(error?.message || String(error)))}>Compact</button>
      ${capabilities?.features?.sessionArchive ? html`<button class="header-action desktop-session-action" disabled=${!connected || Boolean(busy)} onClick=${archive}>${session.archivedAt ? "Reopen" : "Settle"}</button>` : null}
      <details class="session-more-menu">
        <summary class="header-action">More ▾</summary>
        <div class="session-more-menu__popup">
          <button disabled=${!connected} onClick=${() => closeDetailsAnd(rename)}>Rename</button>
          <button disabled=${!connected} onClick=${() => closeDetailsAnd(() => controller.patchSession(session.id, { pinned: !session.pinned }).catch((error) => controller.notice(error?.message || String(error))))}>${session.pinned ? "Unpin" : "Pin"}</button>
          <button disabled=${!connected || Boolean(busy)} onClick=${() => closeDetailsAnd(() => controller.compact(session.id).catch((error) => controller.notice(error?.message || String(error))))}>Compact</button>
          ${capabilities?.features?.sessionArchive ? html`<button disabled=${!connected || Boolean(busy)} onClick=${() => closeDetailsAnd(archive)}>${session.archivedAt ? "Reopen" : "Settle"}</button>` : null}
        </div>
      </details>
    </div>
  </header>`;
}

function inspectorLabel(mode) {
  return ({ tree: "Tree", tool: "Tool", process: "Processes", tools: "Tools", skills: "Skills", mcp: "MCP", context: "Info", file: "File" })[mode] || "Open";
}

function closeDetailsAnd(action) {
  const active = document.activeElement?.closest?.("details");
  if (active) active.open = false;
  void action();
}

function lastPath(path) {
  return path?.split("/").filter(Boolean).at(-1) || path || "Unknown location";
}
