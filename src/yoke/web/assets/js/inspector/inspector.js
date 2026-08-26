import { html } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { ContextInspector, FileInspector, McpInspector, SkillsInspector, ToolsInspector } from "./config.js";
import { ProcessInspector } from "./process.js";
import { ToolInspector } from "./tool.js";
import { TreeInspector } from "./tree.js";

const TITLES = { tree: "Session tree", tool: "Tool inspector", process: "Processes", tools: "Tools", skills: "Skills", mcp: "MCP", context: "Session info", file: "File detail" };

export function Inspector() {
  const inspector = useStore((state) => state.ui.inspector);
  const sessionID = useStore((state) => state.ui.selectedSessionID);
  const session = useStore((state) => sessionID ? state.sessions[sessionID] : null);
  const data = useStore((state) => sessionID ? state.sessionData[sessionID] : null);
  const capabilities = useStore((state) => state.capabilities);
  if (!inspector || !sessionID || !session) return null;
  return html`<aside class="inspector" aria-label=${TITLES[inspector.mode] || "Inspector"}>
    <header class="inspector__header"><div><span class="inspector__eyebrow">${session.title || session.id}</span><h1>${TITLES[inspector.mode] || "Inspector"}</h1></div><button class="icon-button" aria-label="Close inspector" onClick=${() => controller.closeInspector()}>×</button></header>
    <div class="inspector__body">
      ${inspector.mode === "tree" ? html`<${TreeInspector} sessionID=${sessionID} data=${data} />` : null}
      ${inspector.mode === "tool" ? html`<${ToolInspector} sessionID=${sessionID} inspector=${inspector} data=${data} />` : null}
      ${inspector.mode === "process" ? html`<${ProcessInspector} sessionID=${sessionID} data=${data} capabilities=${capabilities} />` : null}
      ${inspector.mode === "tools" ? html`<${ToolsInspector} sessionID=${sessionID} data=${data} />` : null}
      ${inspector.mode === "skills" ? html`<${SkillsInspector} sessionID=${sessionID} data=${data} />` : null}
      ${inspector.mode === "mcp" ? html`<${McpInspector} sessionID=${sessionID} data=${data} />` : null}
      ${inspector.mode === "context" ? html`<${ContextInspector} session=${session} data=${data} />` : null}
      ${inspector.mode === "file" ? html`<${FileInspector} data=${data} />` : null}
    </div>
  </aside>`;
}
