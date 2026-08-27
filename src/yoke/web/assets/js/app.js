import { html, useEffect } from "../vendor/htm-preact.js";
import { AuthScreen } from "./components/auth.js";
import { CommandPalette } from "./components/command-palette.js";
import { Sidebar } from "./components/sidebar.js";
import { Inspector } from "./inspector/inspector.js";
import { installKeybindings } from "./lib/keyboard.js";
import { MainView } from "./session/session-view.js";
import { controller } from "./state/controller.js";
import { useStore } from "./state/hooks.js";

export function App() {
  const authRequired = useStore((state) => state.auth.required);
  const capabilities = useStore((state) => state.capabilities);
  const connection = useStore((state) => state.connection);
  const sidebarOpen = useStore((state) => state.ui.sidebarOpen);
  const inspector = useStore((state) => state.ui.inspector);
  const notice = useStore((state) => state.ui.notice);

  useEffect(() => {
    const saved = Number(localStorage.getItem("yoke.web.sidebarWidth"));
    if (Number.isFinite(saved) && saved >= 230 && saved <= 420) {
      document.documentElement.style.setProperty("--sidebar-width", `${saved}px`);
    }
    return installKeybindings({
      palette: () => controller.togglePalette(true),
      newSession: () => controller.createDraft(),
      toggleSidebar: () => controller.toggleSidebar(),
      escape: () => controller.escape(),
      interrupt: () => controller.interruptSelectedSession(),
      switchSession: (delta) => controller.switchSession(delta),
    });
  }, []);

  if (authRequired) return html`<${AuthScreen} />`;
  if (!capabilities) return html`<div class="boot-screen"><div class="boot-mark">Y</div><div>Connecting to Yoke…</div>${connection.error ? html`<div class="inline-error">${connection.error}</div>` : null}</div>`;

  return html`<div class=${`app-shell ${inspector ? "has-inspector" : ""}`}>
    <${Sidebar} />
    ${sidebarOpen ? html`<button class="sidebar-backdrop" aria-label="Close sessions" onClick=${() => controller.toggleSidebar()}></button>` : null}
    <div class="workspace">
      ${!connection.current ? html`<div class="connection-banner" role="status"><span class="connection-banner__dot"></span><span>${connection.status === "resyncing" ? "Resynchronizing Yoke state…" : connection.status === "disconnected" ? "Connection interrupted. Reconnecting…" : "Connecting…"}</span>${connection.error ? html`<span class="muted">${connection.error}</span>` : null}</div>` : null}
      <${MainView} />
      <${Inspector} />
    </div>
    <${CommandPalette} />
    ${notice ? html`<div class="notice-toast" role="status">${notice}</div>` : null}
  </div>`;
}
