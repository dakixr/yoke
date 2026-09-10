import { html } from "../../vendor/htm-preact.js";
import { useInspectorPreferences } from "./state/preferences.js";
import { ToolsView, SkillsView } from "./config/catalog.js";
import { McpView } from "./config/mcp.js";
import { ContextView, FileView } from "./config/documents.js";

export function ToolsInspector({ sessionID, data }) {
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "tools", { search: "", filter: "all", scrollTop: 0 });
  return html`<${ToolsView} key=${sessionID} sessionID=${sessionID} data=${data} preferences=${preferences} patchPreferences=${patchPreferences} />`;
}

export function SkillsInspector({ sessionID, data }) {
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "skills", { search: "", scrollTop: 0 });
  return html`<${SkillsView} key=${sessionID} sessionID=${sessionID} data=${data} preferences=${preferences} patchPreferences=${patchPreferences} />`;
}

export function McpInspector({ sessionID, data }) {
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "mcp", { search: "", scrollTop: 0 });
  return html`<${McpView} key=${sessionID} sessionID=${sessionID} data=${data} preferences=${preferences} patchPreferences=${patchPreferences} />`;
}

export function ContextInspector({ session, data }) {
  const [preferences, patchPreferences] = useInspectorPreferences(session.id, "context", { scrollTop: 0 });
  return html`<${ContextView} key=${session.id} session=${session} data=${data} preferences=${preferences} patchPreferences=${patchPreferences} />`;
}

export function FileInspector({ sessionID, data, inspector }) {
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "file", { search: "", wrap: true, scrollTop: 0, path: null });
  return html`<${FileView} key=${`${sessionID}:${data?.fileDetail?.path || "loading"}`} data=${data} inspector=${inspector} preferences=${preferences} patchPreferences=${patchPreferences} />`;
}
