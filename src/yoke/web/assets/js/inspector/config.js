import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function ToolsInspector({ sessionID, data }) {
  if (!data?.tools) return html`<div class="inspector-loading">Loading tools…</div>`;
  return html`<div class="inspector-stack config-inspector"><div class="inspector-meta"><span>${data.tools.length} discovered tools</span><span>Session-local toggles</span></div><div class="config-list">
    ${data.tools.map((tool) => html`<label class="config-row"><span class="config-row__main"><strong>${tool.name}</strong><span>${tool.description}</span><code>${tool.source}${tool.capabilityID ? ` · ${tool.capabilityID}` : ""}</code></span><input type="checkbox" checked=${tool.enabled} onChange=${(event) => controller.toggleTool(sessionID, tool.name, event.currentTarget.checked)} /></label>`)}
  </div></div>`;
}

export function SkillsInspector({ sessionID, data }) {
  if (!data?.skills) return html`<div class="inspector-loading">Loading skills…</div>`;
  const activeNames = new Set((data.skills.active || []).map((skill) => skill.name));
  return html`<div class="inspector-stack config-inspector"><div class="inspector-meta"><span>${activeNames.size} active · ${(data.skills.available || []).length} available</span><span>Activate skills for this session</span></div><div class="config-list">
    ${(data.skills.available || []).map((skill) => html`<div class="config-row"><span class="config-row__main"><strong>${skill.name}</strong><span>${skill.description}</span><code>${skill.sourcePath}</code></span>${activeNames.has(skill.name) ? html`<span class="status-pill status-pill--ok">active</span>` : html`<button onClick=${() => controller.activateSkill(sessionID, skill.name)}>Activate</button>`}</div>`)}
  </div></div>`;
}

export function McpInspector({ sessionID, data }) {
  if (!data?.mcp) return html`<div class="inspector-loading">Loading MCP servers…</div>`;
  return html`<div class="inspector-stack"><div class="inspector-meta"><span>${data.mcp.length} configured servers</span><span>Choose scope before persisting changes</span></div><div class="mcp-grid">${data.mcp.map((server) => html`<${McpServer} key=${server.name} sessionID=${sessionID} server=${server} />`)}</div></div>`;
}

function McpServer({ sessionID, server }) {
  const [scope, setScope] = useState("session");
  const disabled = new Set(server.disabledTools || []);
  const toolEnabled = (name) => server.enabledTools ? server.enabledTools.includes(name) : !disabled.has(name);
  const toggleTool = (name, enabled) => {
    const nextDisabled = new Set(server.disabledTools || []);
    if (enabled) nextDisabled.delete(name); else nextDisabled.add(name);
    return controller.patchMcp(sessionID, server.name, { scope, disabledTools: [...nextDisabled] });
  };
  return html`<section class="mcp-server">
    <div class="mcp-server__heading"><div><strong>${server.name}</strong><span>${server.transport} · configured ${server.scope === "unknown" ? "outside known scopes" : server.scope === "repo" ? "in repository" : "globally"}</span></div><span class=${`status-pill ${server.enabled ? "status-pill--ok" : ""}`}>${server.status}</span></div>
    ${server.error ? html`<div class="inline-error">${server.error}</div>` : null}
    <div class="mcp-policy"><label><span>Apply changes to</span><select value=${scope} aria-label=${`Policy scope for ${server.name}`} onChange=${(event) => setScope(event.currentTarget.value)}><option value="session">This session</option><option value="repo">Repository</option><option value="global">Global config</option></select></label><button onClick=${() => controller.patchMcp(sessionID, server.name, { scope, enabled: !server.enabled })}>${server.enabled ? "Disable" : "Enable"}</button></div>
    ${server.tools?.length ? html`<div class="mcp-tools">${server.tools.map((tool) => html`<label><input type="checkbox" checked=${toolEnabled(tool.name)} onChange=${(event) => toggleTool(tool.name, event.currentTarget.checked)} /><span><strong>${tool.name}</strong>${tool.description ? html`<small>${tool.description}</small>` : null}</span></label>`)}</div>` : html`<div class="muted">No inspected tools${server.truncated ? " in retained page" : ""}.</div>`}
  </section>`;
}

export function ContextInspector({ session, data }) {
  return html`<div class="inspector-stack context-inspector"><section class="context-summary"><div class="inspector-section-title">Session</div><dl class="detail-grid"><dt>ID</dt><dd><code>${session.id}</code></dd><dt>Location</dt><dd>${session.location.directory}</dd><dt>Provider</dt><dd>${session.selection?.provider || "—"}</dd><dt>Model</dt><dd>${session.selection?.model || "—"}</dd><dt>Effort</dt><dd>${session.selection?.reasoningEffort || "—"}</dd><dt>Created</dt><dd>${session.time?.created || "—"}</dd><dt>Updated</dt><dd>${session.time?.updated || "—"}</dd><dt>Archived</dt><dd>${session.archivedAt || "—"}</dd><dt>Tree</dt><dd>${session.tree?.entryCount || 0} entries</dd></dl></section>
    <section><div class="inspector-section-title">Recent model-visible context</div>${data?.context?.truncated ? html`<div class="inspector-meta"><span>Bounded view</span><span>${data.context.retainedEntries} of ${data.context.totalEntries} active entries · ${Math.round((data.context.retainedChars || 0) / 1000)}k / ${Math.round((data.context.maxChars || 0) / 1000)}k chars</span></div>` : null}${data?.context?.messages?.length ? data.context.messages.map((message) => html`<div class="context-line"><span>${message.role}${message.phase ? ` · ${message.phase}` : ""}</span><p>${message.content?.filter((part) => part.type === "text").map((part) => part.text).join("\n") || "[non-text content]"}</p></div>`) : html`<div class="muted">No context loaded.</div>`}</section>
  </div>`;
}

export function FileInspector({ data }) {
  if (!data?.fileDetail) return html`<div class="inspector-loading">Loading file…</div>`;
  return html`<div class="inspector-stack"><div class="inspector-title-row"><h2>${data.fileDetail.path}</h2></div><pre class="file-pre">${data.fileDetail.content}</pre></div>`;
}
