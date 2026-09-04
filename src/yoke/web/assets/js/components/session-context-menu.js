import { html, useEffect, useRef } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { copyText } from "../lib/clipboard.js";
import { hasPendingQueue } from "./sidebar-status.js";

const MENU_WIDTH = 208;

export function SessionContextMenu({ session, location, runtime, capabilities, position, onClose }) {
  const menuRef = useRef(null);
  const busy = runtime?.state && runtime.state !== "idle" && runtime.state !== "error";
  const pending = hasPendingQueue(session.queue);
  const directory = session.location?.directory || "";
  const branch = location?.git?.branch || null;
  const left = Math.max(8, Math.min(position.x, window.innerWidth - MENU_WIDTH - 8));
  const top = Math.max(8, Math.min(position.y, window.innerHeight - 360));

  useEffect(() => {
    const closeOutside = (event) => {
      if (!menuRef.current?.contains(event.target)) onClose();
    };
    const closeOnKey = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    const close = () => onClose();
    document.addEventListener("pointerdown", closeOutside, true);
    document.addEventListener("keydown", closeOnKey, true);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    queueMicrotask(() => menuRef.current?.querySelector("button:not(:disabled)")?.focus());
    return () => {
      document.removeEventListener("pointerdown", closeOutside, true);
      document.removeEventListener("keydown", closeOnKey, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [onClose]);

  const run = async (action, { keepOpen = false } = {}) => {
    if (!keepOpen) onClose();
    try {
      await action();
    } catch (error) {
      controller.notice(error?.message || String(error));
    }
  };
  const rename = () => {
    const title = window.prompt("Session title", session.title || "");
    if (title === null) return;
    void run(() => controller.patchSession(session.id, { title }));
  };
  const regenerate = async () => {
    onClose();
    controller.pendingNotice("Regenerating title…");
    try {
      const updated = await controller.regenerateTitle(session.id);
      controller.notice(`Title updated to “${updated.title}”.`);
    } catch (error) {
      controller.notice(error?.message || String(error));
    }
  };
  const copy = (value, label) => void run(async () => {
    await copyText(value);
    controller.notice(`${label} copied.`);
  });

  return html`
    <div
      ref=${menuRef}
      class="session-context-menu"
      role="menu"
      aria-label="Session actions"
      style=${`left:${left}px;top:${top}px;width:${MENU_WIDTH}px`}
      onContextMenu=${(event) => event.preventDefault()}
    >
      <button role="menuitem" onClick=${() => void run(() => Promise.resolve(controller.createDraft({ location: directory })))}>
        <span>New session here</span>
      </button>
      <button role="menuitem" onClick=${() => void run(() => controller.patchSession(session.id, { pinned: !session.pinned }))}>
        <span>${session.pinned ? "Unpin session" : "Pin session"}</span>
      </button>
      ${capabilities?.features?.sessionArchive ? html`
        <button role="menuitem" disabled=${Boolean(busy || (!session.archivedAt && pending))} onClick=${() => void run(() => controller.patchSession(session.id, { archived: !session.archivedAt }))}>
          <span>${session.archivedAt ? "Reopen session" : "Settle session"}</span>
        </button>
      ` : null}
      <div class="context-menu-separator" role="separator"></div>
      <button role="menuitem" onClick=${rename}>
        <span>Rename session</span>
      </button>
      ${capabilities?.features?.sessionTitleRegeneration ? html`
        <button role="menuitem" onClick=${() => void regenerate()}>
          <span>Regenerate title</span>
        </button>
      ` : null}
      <div class="context-menu-separator" role="separator"></div>
      <button role="menuitem" onClick=${() => copy(directory, "Path")}>
        <span>Copy path</span>
      </button>
      ${branch ? html`
        <button role="menuitem" onClick=${() => copy(branch, "Branch")}>
          <span>Copy branch</span>
        </button>
      ` : null}
      <button role="menuitem" onClick=${() => copy(session.id, "Session ID")}>
        <span>Copy session ID</span>
      </button>
    </div>
  `;
}
