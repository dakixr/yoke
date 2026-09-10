import { html } from "../../../vendor/htm-preact.js";
import { controller } from "../../state/controller.js";
import { Feedback, LoadState, ScrollArea, useActions } from "./feedback.js";
import { groupTools, skillSections } from "./logic.js";

export function ToolsView({ sessionID, data, preferences, patchPreferences }) {
  const { feedback, run } = useActions();
  if (!data?.tools) return html`<${LoadState} error=${data?.inspectorErrors?.tools} label="Loading tools…" />`;
  const groups = groupTools(data.tools, preferences.search, preferences.filter, feedback);
  return html`<div class="support-config">
    <p class="support-scope">Tool changes apply immediately to this session only.</p>
    <div class="support-toolbar">
      <input type="search" aria-label="Search tools" placeholder="Search tools" value=${preferences.search} onInput=${(event) => patchPreferences({ search: event.currentTarget.value })} />
      <select aria-label="Tool enabled state" value=${preferences.filter} onChange=${(event) => patchPreferences({ filter: event.currentTarget.value })}>
        <option value="all">All tools</option><option value="enabled">Enabled</option><option value="disabled">Disabled</option>
      </select>
      <span>${data.tools.filter((tool) => tool.enabled).length} enabled / ${data.tools.length} discovered</span>
    </div>
    <${ScrollArea} preferences=${preferences} patchPreferences=${patchPreferences}>
      ${groups.map((group) => html`<section key=${group.source} class="support-group"><h2>${group.source}<span>${group.tools.length}</span></h2>
        ${group.tools.map((tool) => html`<div key=${tool.name} class="support-row">
          <details class="support-row__main"><summary><strong>${tool.name}</strong></summary>
            <p>${tool.description || "No description provided."}</p>
            <dl class="support-facts"><dt>Tool ID</dt><dd><code>${tool.name}</code></dd>${tool.capabilityID ? html`<dt>Capability ID</dt><dd><code>${tool.capabilityID}</code></dd>` : null}${tool.sourcePath ? html`<dt>Source path</dt><dd><code>${tool.sourcePath}</code></dd>` : null}</dl>
          </details>
          <div class="support-row__control"><label><input type="checkbox" aria-label=${`Enable ${tool.name} for this session`} checked=${tool.enabled} disabled=${feedback[tool.name]?.pending} onChange=${(event) => {
            const enabled = event.currentTarget.checked;
            void run(tool.name, () => controller.toggleTool(sessionID, tool.name, enabled));
          }} /><span>${tool.enabled ? "Enabled" : "Disabled"}</span></label><${Feedback} state=${feedback[tool.name]} /></div>
        </div>`)}
      </section>`)}
      ${!groups.length ? html`<p class="support-empty">${data.tools.length ? "No tools match this search and filter." : "No tools were discovered for this session."}</p>` : null}
    <//>
  </div>`;
}

export function SkillsView({ sessionID, data, preferences, patchPreferences }) {
  const { feedback, run } = useActions();
  if (!data?.skills) return html`<${LoadState} error=${data?.inspectorErrors?.skills} label="Loading skills…" />`;
  const sections = skillSections(data.skills, preferences.search);
  const row = (skill, active) => html`<div key=${skill.name} class="support-row support-skill">
    <div class="support-row__main"><strong>${skill.name}</strong><p>${skill.description || "No description provided."}</p>
      ${skill.sourcePath ? html`<div class="support-skill__preview"><button onClick=${() => {
        void run(`preview:${skill.name}`, () => controller.openInspector("file", { path: skill.sourcePath, from: { mode: "skills" } }), "");
      }}>Preview current instructions</button><${Feedback} state=${feedback[`preview:${skill.name}`]} pendingLabel="Opening…" /><details><summary>Source path</summary><code>${skill.sourcePath}</code></details></div>` : html`<span class="support-secondary">No instruction file path available.</span>`}
    </div>
    <div class="support-row__control">${active ? html`<span>Active</span>` : html`<button disabled=${feedback[skill.name]?.pending} onClick=${() => { void run(skill.name, () => controller.activateSkill(sessionID, skill.name), "Activated"); }}>Activate</button>`}<${Feedback} state=${feedback[skill.name]} pendingLabel="Activating…" /></div>
  </div>`;
  return html`<div class="support-config">
    <p class="support-scope">Activate adds a skill to this session. Preview reads its current instruction file without activating it, where filesystem access permits. Deactivation is not available here.</p>
    <${ScrollArea} preferences=${preferences} patchPreferences=${patchPreferences}>
      <section class="support-group"><h2>Active in this session<span>${sections.active.length}</span></h2>
        ${sections.active.length ? sections.active.map((skill) => row(skill, true)) : html`<p class="support-empty">No active skills.</p>`}
      </section>
      <section class="support-group"><h2>Available to activate</h2>
        <div class="support-toolbar"><input type="search" aria-label="Search available skills" placeholder="Search available skills" value=${preferences.search} onInput=${(event) => patchPreferences({ search: event.currentTarget.value })} /></div>
        ${sections.available.length ? sections.available.map((skill) => row(skill, false)) : html`<p class="support-empty">${preferences.search ? "No available skills match this search." : "No other skills available."}</p>`}
      </section>
    <//>
  </div>`;
}
