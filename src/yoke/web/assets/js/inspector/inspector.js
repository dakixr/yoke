import { html, useEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { trapFocus } from "../lib/focus.js";
import { useModalFocus } from "../lib/modal-focus.js";
import { ContextInspector, FileInspector, McpInspector, SkillsInspector, ToolsInspector } from "./config.js";
import { ProcessInspector } from "./process.js";
import { ToolInspector } from "./tool.js";
import { TreeInspector } from "./tree.js";

const PRIMARY_VIEWS = [
  { mode: "tool", label: "Tool activity", feature: "toolInspector" },
  { mode: "tree", label: "Tree", feature: "sessionTree" },
  { mode: "process", label: "Processes", feature: "processInspector" },
  { mode: "context", label: "Context" },
  { mode: "configuration", label: "Configuration" },
];
const CONFIGURATION_VIEWS = [
  { mode: "tools", label: "Tools" },
  { mode: "skills", label: "Skills", feature: "skills" },
  { mode: "mcp", label: "MCP servers", feature: "mcp" },
];
const RESOURCE_KEYS = { tool: "toolCalls", tree: "tree", process: "processes", tools: "tools", skills: "skills", mcp: "mcp", context: "context", file: "fileDetail" };

export function Inspector() {
  const inspector = useStore((state) => state.ui.inspector);
  const sessionID = useStore((state) => state.ui.selectedSessionID);
  const session = useStore((state) => sessionID ? state.sessions[sessionID] : null);
  const data = useStore((state) => sessionID ? state.sessionData[sessionID] : null);
  const capabilities = useStore((state) => state.capabilities);
  if (!inspector || !sessionID || !session) return null;
  return html`<${InspectorDialog} key=${sessionID} inspector=${inspector} sessionID=${sessionID} session=${session} data=${data} capabilities=${capabilities} />`;
}

function InspectorDialog({ inspector, sessionID, session, data, capabilities }) {
  const dialog = useRef(null);
  const connection = useStore((state) => state.connection);
  const [help, setHelp] = useState(false);
  useModalFocus(dialog);
  const modes = PRIMARY_VIEWS.filter((item) => !item.feature || capabilities?.features?.[item.feature]);
  const configurationModes = CONFIGURATION_VIEWS.filter((item) => !item.feature || capabilities?.features?.[item.feature]);
  const configuration = CONFIGURATION_VIEWS.some((item) => item.mode === inspector.mode);
  const originMode = inspector.mode === "file" ? inspector.from?.mode || "tool" : inspector.mode;
  const activeView = CONFIGURATION_VIEWS.some((item) => item.mode === originMode) ? "configuration" : originMode;
  const error = data?.inspectorErrors?.[inspector.mode];
  const loaded = data?.[RESOURCE_KEYS[inspector.mode]] != null;
  const open = (mode) => {
    setHelp(false);
    const selected = mode === "configuration"
      ? configurationModes.find((item) => item.mode === data?.inspectorConfigurationMode)?.mode || "tools"
      : mode;
    void controller.openInspector(selected);
  };
  const onKeyDown = (event) => {
    if (event.defaultPrevented) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (help) setHelp(false);
      else if (inspector.mode === "file" && inspector.from) void controller.backInspector();
      else controller.closeInspector();
      return;
    }
    trapFocus(dialog.current, event);
  };
  return html`<div class="inspector-backdrop" onMouseDown=${(event) => {
    if (event.target === event.currentTarget) controller.closeInspector();
  }}>
    <section class="inspector" role="dialog" aria-modal="true" aria-labelledby="inspector-session-title" tabindex="-1" ref=${dialog} onKeyDown=${onKeyDown}>
      <header class="inspector__header">
        <div class="inspector__heading">
          <span class="inspector__eyebrow">Session inspector</span>
          <h1 id="inspector-session-title" title=${session.title || session.id}>${session.title || session.id}</h1>
        </div>
        <div class="inspector__header-actions">
          ${!connection.current ? html`<span class="inspector-connection" role="status">${connection.status === "disconnected" ? "Disconnected. Showing saved data." : "Synchronizing"}</span>` : null}
          <button class="inspector-help-toggle" aria-expanded=${help} aria-controls="inspector-help" onClick=${() => setHelp((value) => !value)}>Keyboard</button>
          <button class="icon-button inspector__close" aria-label="Close inspector" title="Close inspector, Escape" onClick=${() => controller.closeInspector()}>×</button>
        </div>
      </header>
      <${ViewTabs} items=${modes} active=${activeView} onOpen=${open} />
      <div class="inspector__content">
        ${help ? html`<div id="inspector-help" class="inspector-help">
          <p><kbd>Tab</kbd> moves between controls. <kbd>Esc</kbd> closes the inspector. <kbd>⌘ K</kbd> or <kbd>Ctrl K</kbd> opens commands.</p>
          <p>In Tree, arrow keys choose a destination, <kbd>Enter</kbd> continues from the loaded destination, and <kbd>Esc</kbd> clears the destination first.</p>
        </div>` : null}
        ${configuration ? html`<${ViewTabs} items=${configurationModes} active=${inspector.mode} onOpen=${open} secondary />` : null}
        ${error ? html`<div class="inspector-load-error" role="alert"><span>${error}</span><button onClick=${() => controller.openInspector(inspector.mode, inspector)}>Retry</button></div>` : null}
        <div id="inspector-view" role="tabpanel" aria-label=${configurationModes.find((item) => item.mode === inspector.mode)?.label || modes.find((item) => item.mode === activeView)?.label || "File"} class=${`inspector__body inspector__body--${inspector.mode}`}>
          ${error && !loaded && !["file", "tool"].includes(inspector.mode) ? html`<div class="inspector-empty">This view could not be loaded. Retry when the connection is available.</div>` : html`
            ${inspector.mode === "tree" ? html`<${TreeInspector} sessionID=${sessionID} data=${data} />` : null}
            ${inspector.mode === "tool" ? html`<${ToolInspector} sessionID=${sessionID} inspector=${inspector} data=${data} />` : null}
            ${inspector.mode === "process" ? html`<${ProcessInspector} sessionID=${sessionID} inspector=${inspector} data=${data} capabilities=${capabilities} />` : null}
            ${inspector.mode === "tools" ? html`<${ToolsInspector} sessionID=${sessionID} data=${data} />` : null}
            ${inspector.mode === "skills" ? html`<${SkillsInspector} sessionID=${sessionID} data=${data} />` : null}
            ${inspector.mode === "mcp" ? html`<${McpInspector} sessionID=${sessionID} data=${data} />` : null}
            ${inspector.mode === "context" ? html`<${ContextInspector} sessionID=${sessionID} session=${session} data=${data} />` : null}
            ${inspector.mode === "file" ? html`<${FileInspector} sessionID=${sessionID} inspector=${inspector} data=${data} />` : null}
          `}
        </div>
      </div>
    </section>
  </div>`;
}

function ViewTabs({ items, active, onOpen, secondary = false }) {
  const [focused, setFocused] = useState(active);
  useEffect(() => setFocused(active), [active]);
  const onKeyDown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    const nodes = [...event.currentTarget.querySelectorAll('[role="tab"]')];
    const index = nodes.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? nodes.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + nodes.length) % nodes.length;
    nodes[next]?.focus();
  };
  return html`<nav class=${secondary ? "inspector-subnav" : "inspector-nav"} role="tablist" aria-label=${secondary ? "Configuration views" : "Inspector views"} onKeyDown=${onKeyDown}>
    ${items.map((item) => html`<button key=${item.mode} type="button" role="tab"
      aria-selected=${active === item.mode} aria-controls="inspector-view"
      tabindex=${focused === item.mode ? 0 : -1}
      class=${active === item.mode ? "is-active" : ""}
      onFocus=${() => setFocused(item.mode)} onClick=${() => onOpen(item.mode)}
    >${item.label}</button>`)}
  </nav>`;
}
