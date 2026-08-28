import { html } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { ContextInspector, FileInspector, McpInspector, SkillsInspector, ToolsInspector } from "./config.js";
import { ProcessInspector } from "./process.js";
import { ToolInspector } from "./tool.js";
import { TreeInspector } from "./tree.js";

const TITLES = { tree: "Conversation graph", tool: "Tool activity", process: "Process inspector", tools: "Tools", skills: "Skills", mcp: "MCP", context: "Session info", file: "File detail" };

const INSPECTOR_MODES = [
  { mode: "tree", label: "Tree", feature: "sessionTree" },
  { mode: "tool", label: "Tool activity", feature: "toolInspector" },
  { mode: "process", label: "Processes", feature: "processInspector" },
  { mode: "tools", label: "Tools" },
  { mode: "skills", label: "Skills", feature: "skills" },
  { mode: "mcp", label: "MCP", feature: "mcp" },
  { mode: "context", label: "Session info" },
];

export function Inspector() {
  const inspector = useStore((state) => state.ui.inspector);
  const sessionID = useStore((state) => state.ui.selectedSessionID);
  const session = useStore((state) => sessionID ? state.sessions[sessionID] : null);
  const data = useStore((state) => sessionID ? state.sessionData[sessionID] : null);
  const capabilities = useStore((state) => state.capabilities);
  if (!inspector || !sessionID || !session) return null;
  const modes = INSPECTOR_MODES.filter((item) => !item.feature || capabilities?.features?.[item.feature]);
  return html`<div class="inspector-backdrop" onMouseDown=${(event) => {
    if (event.target === event.currentTarget) controller.closeInspector();
  }}>
    <section class="inspector" role="dialog" aria-modal="true" aria-label=${TITLES[inspector.mode] || "Inspector"}>
      <header class="inspector__header">
        <div class="inspector__heading">
          <span class="inspector__eyebrow">${session.title || session.id}</span>
          <h1>${TITLES[inspector.mode] || "Inspector"}</h1>
        </div>
        <button class="icon-button inspector__close" aria-label="Close inspector" title="Close · Esc" onClick=${() => controller.closeInspector()}>×</button>
      </header>
      <nav class="inspector-nav" aria-label="Inspector views">
        ${modes.map((item) => html`<button
          key=${item.mode}
          class=${inspector.mode === item.mode ? "is-active" : ""}
          aria-current=${inspector.mode === item.mode ? "page" : undefined}
          onClick=${() => controller.openInspector(item.mode)}
        >${item.label}</button>`)}
      </nav>
      <div class=${`inspector__body inspector__body--${inspector.mode}`}>
        ${inspector.mode === "tree" ? html`<${TreeInspector} sessionID=${sessionID} data=${data} />` : null}
        ${inspector.mode === "tool" ? html`<${ToolInspector} sessionID=${sessionID} inspector=${inspector} data=${data} />` : null}
        ${inspector.mode === "process" ? html`<${ProcessInspector} sessionID=${sessionID} data=${data} capabilities=${capabilities} />` : null}
        ${inspector.mode === "tools" ? html`<${ToolsInspector} sessionID=${sessionID} data=${data} />` : null}
        ${inspector.mode === "skills" ? html`<${SkillsInspector} sessionID=${sessionID} data=${data} />` : null}
        ${inspector.mode === "mcp" ? html`<${McpInspector} sessionID=${sessionID} data=${data} />` : null}
        ${inspector.mode === "context" ? html`<${ContextInspector} session=${session} data=${data} />` : null}
        ${inspector.mode === "file" ? html`<${FileInspector} data=${data} />` : null}
      </div>
      <footer class="inspector__footer"><span>Esc close</span><span>Ctrl+X T tree</span><span>Ctrl+X Ctrl+P processes</span></footer>
    </section>
  </div>`;
}
