import { html, useState } from "../../../vendor/htm-preact.js";
import { controller } from "../../state/controller.js";
import { Feedback, LoadState, ScrollArea, useActions } from "./feedback.js";
import { matchesSearch, mcpCounts, mcpToolEnabled, mcpToolPatch } from "./logic.js";

export function McpView({ sessionID, data, preferences, patchPreferences }) {
  if (!data?.mcp) return html`<${LoadState} error=${data?.inspectorErrors?.mcp} label="Loading MCP servers…" />`;
  const servers = data.mcp.filter((server) => matchesSearch(preferences.search, server.name, server.status));
  return html`<div class="support-config">
    <div class="support-toolbar"><input type="search" aria-label="Search MCP servers" placeholder="Search servers or connection status" value=${preferences.search} onInput=${(event) => patchPreferences({ search: event.currentTarget.value })} /><span>${data.mcp.length} configured servers</span></div>
    <${ScrollArea} preferences=${preferences} patchPreferences=${patchPreferences}>
      ${data.mcp.map((server) => html`<${McpServer} key=${server.name} sessionID=${sessionID} server=${server} hidden=${!servers.includes(server)} search=${preferences.inventorySearch?.[server.name] || ""} setSearch=${(search) => patchPreferences((current) => ({ inventorySearch: { ...current.inventorySearch, [server.name]: search } }))} />`)}
      ${!servers.length ? html`<p class="support-empty">${data.mcp.length ? "No servers match this search." : "No MCP servers configured."}</p>` : null}
    <//>
  </div>`;
}

function McpServer({ sessionID, server, hidden, search = "", setSearch = () => {} }) {
  const [scope, setScope] = useState("session");
  const [draft, setDraft] = useState({});
  const { feedback, run, clear } = useActions();
  const pending = Object.values(feedback).some((state) => state.pending);
  const effective = { ...server, ...draft };
  const counts = mcpCounts(server);
  const dirty = Object.keys(draft).length > 0;
  const change = (key, patch) => {
    if (scope !== "session") { setDraft((value) => ({ ...value, ...patch })); clear(); return; }
    void run(key, () => controller.patchMcp(sessionID, server.name, { scope: "session", ...patch }));
  };
  const tools = (server.tools || []).filter((tool) => matchesSearch(search, tool.name, tool.description));
  const origin = server.scope === "repo" ? "Repository config" : server.scope === "global" ? "Global config" : "Outside known config scopes";
  const failed = Boolean(server.error) || ["error", "failed"].includes(server.status);
  return html`<section class="support-mcp" hidden=${hidden}>
    <header class="support-mcp__heading"><h2>${server.name}</h2><span class=${failed ? "support-failure" : "support-secondary"}>Connection: ${server.status || "not inspected"}</span></header>
    ${server.error ? html`<p class="support-feedback--error" role="alert">${server.error}</p>` : null}
    <div class="support-mcp__summary"><span>Effective state: ${server.enabled ? "Enabled" : "Disabled"}</span><span>${counts.enabled} / ${counts.total} ${server.truncated ? "loaded " : "inspected "}capabilities enabled</span></div>
    ${server.truncated ? html`<p class="support-secondary">Inventory is truncated. Counts cover only the loaded tools.</p>` : null}
    <details class="support-origin"><summary>Configured origin: ${origin}</summary><dl class="support-facts"><dt>Transport</dt><dd>${server.transport}</dd><dt>Config path</dt><dd><code>${server.sourcePath || "Not reported"}</code></dd></dl></details>
    <div class="support-mcp__policy">
      <label>Apply scope<select aria-label=${`Apply scope for ${server.name}`} value=${scope} disabled=${pending} onChange=${(event) => { setScope(event.currentTarget.value); setDraft({}); clear(); }}><option value="session">This session</option><option value="repo">Repository</option><option value="global">Global config</option></select></label>
      <div class="support-row__control"><label><input type="checkbox" checked=${effective.enabled} disabled=${pending} aria-label=${`Enable server ${server.name}`} onChange=${(event) => change("server", { enabled: event.currentTarget.checked })} />${effective.enabled ? "Enabled" : "Disabled"}${scope !== "session" && dirty ? " in draft" : ""}</label><${Feedback} state=${feedback.server} /></div>
    </div>
    <p class="support-secondary">${scope === "session" ? "Changes save immediately for this session only." : `Changes are staged until Apply. Writes ${scope === "repo" ? "repository" : "global"} configuration and requires an idle session. Draft starts from this session's effective policy. Changing scope discards the draft.`}</p>
    <details class="support-mcp__inventory"><summary>Capability inventory <span>${server.tools?.length || 0} tools</span></summary>
      <div class="support-toolbar"><input type="search" aria-label=${`Search capabilities for ${server.name}`} placeholder="Search capabilities" value=${search} onInput=${(event) => setSearch(event.currentTarget.value)} /></div>
      ${!effective.enabled ? html`<p class="support-secondary">The server is disabled. Tool policy below is retained but does not enable the server.</p>` : null}
      ${tools.map((tool) => html`<div key=${tool.name} class="support-row"><details class="support-row__main"><summary><strong>${tool.name}</strong></summary><p>${tool.description || "No description provided."}</p><code>${tool.name}</code></details><div class="support-row__control"><label><input type="checkbox" checked=${mcpToolEnabled(effective, tool.name)} disabled=${pending} aria-label=${`Allow ${tool.name} on ${server.name}`} onChange=${(event) => change(`tool:${tool.name}`, mcpToolPatch(effective, tool.name, event.currentTarget.checked))} />Allowed</label><${Feedback} state=${feedback[`tool:${tool.name}`]} /></div></div>`)}
      ${!tools.length ? html`<p class="support-empty">${server.tools?.length ? "No capabilities match this search." : "No inspected capabilities available. Connection status above may explain why."}</p>` : null}
    </details>
    ${scope !== "session" ? html`<div class="support-mcp__apply"><span>${dirty ? "Unsaved draft" : "No pending changes"}</span><button disabled=${!dirty || pending} onClick=${() => { setDraft({}); clear(); }}>Discard</button><button class="primary" disabled=${!dirty || pending} onClick=${async () => {
      if (await run("apply", () => controller.patchMcp(sessionID, server.name, { scope, ...draft }), "Applied")) setDraft({});
    }}>Apply to ${scope === "repo" ? "repository" : "global config"}</button><${Feedback} state=${feedback.apply} pendingLabel="Applying…" /></div>` : null}
  </section>`;
}
