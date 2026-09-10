import { html, useLayoutEffect, useRef, useState } from "../../../vendor/htm-preact.js";
import { controller } from "../../state/controller.js";
import { Feedback, LoadState, ScrollArea, useActions } from "./feedback.js";
import { matchingLines, messagePreview } from "./logic.js";

export function ContextView({ session, data, preferences, patchPreferences }) {
  const context = data?.context;
  return html`<div class="support-config"><${ScrollArea} preferences=${preferences} patchPreferences=${patchPreferences} className="support-context">
    <section class="support-group"><h2>Session properties</h2><p class="support-secondary">Session metadata, separate from the context messages below.</p>
      <dl class="support-facts"><dt>ID</dt><dd><code>${session.id}</code></dd><dt>Location</dt><dd><code>${session.location?.directory || "Not reported"}</code></dd><dt>Provider</dt><dd>${session.selection?.provider || "Not selected"}</dd><dt>Model</dt><dd>${session.selection?.model || "Not selected"}</dd><dt>Effort</dt><dd>${session.selection?.reasoningEffort || "Not specified"}</dd><dt>Created</dt><dd>${session.time?.created || "Not reported"}</dd><dt>Updated</dt><dd>${session.time?.updated || "Not reported"}</dd><dt>Archived</dt><dd>${session.archivedAt || "No"}</dd><dt>Tree</dt><dd>${session.tree?.entryCount ?? 0} entries</dd></dl>
    </section>
    <section class="support-group"><h2>Recent model-visible context</h2><p class="support-scope">A bounded context view, not the full provider prompt. Messages run oldest to newest within the loaded window.</p>
      ${context ? html`<div class="support-context__bounds"><span>${context.messages?.length || 0} messages loaded</span><span>${context.retainedEntries ?? "Unknown"} / ${context.totalEntries ?? "unknown"} active entries retained</span><span>${context.retainedChars?.toLocaleString() ?? "Unknown"} / ${context.maxChars?.toLocaleString() ?? "unknown"} character limit</span><span>${context.truncated ? "Earlier content omitted to fit this window." : "No truncation reported for this window."}</span></div>` : null}
      ${!context ? html`<${LoadState} error=${data?.inspectorErrors?.context} label="Loading context…" />` : context.messages?.length ? html`<ol class="support-context__messages">
        ${context.messages.map((message, index) => html`<li key=${index}><details class="support-message"><summary><span class="support-message__role">${index + 1}. ${message.role}${message.phase ? ` / ${message.phase}` : ""}</span><span class="support-message__preview">${messagePreview(message)}</span><span class="support-secondary">${message.content?.length || 0} blocks</span></summary>
          ${message.toolCallID ? html`<p class="support-secondary">Tool call <code>${message.toolCallID}</code></p>` : null}
          ${(message.content || []).map((part, blockIndex) => html`<div class="support-context__block"><span class="support-caption">Block ${blockIndex + 1}: ${part.type || "non-text"}</span>${part.type === "text" ? html`<pre>${part.text || "[empty text block]"}</pre>` : html`<p>${part.type === "image" ? `Image: ${part.name || "unnamed"}. Image contents are not rendered in this context view.` : "Non-text content. This view does not render its contents."}</p>`}</div>`)}
          ${!message.content?.length ? html`<p class="support-secondary">No content blocks reported.</p>` : null}
        </details></li>`)}
      </ol>` : html`<p class="support-empty">No model-visible messages in the loaded context window.</p>`}
    </section>
  <//></div>`;
}

export function FileView({ data, inspector, preferences, patchPreferences }) {
  const file = data?.fileDetail;
  const viewport = useRef(null);
  const [matchIndex, setMatchIndex] = useState(0);
  const { feedback, run } = useActions();
  const search = preferences.path === file?.path ? preferences.search : "";
  const matches = matchingLines(file?.content || "", search);
  const currentMatch = matches.length ? matches[matchIndex % matches.length] : -1;
  useLayoutEffect(() => {
    if (!file) return;
    if (viewport.current) viewport.current.scrollTop = preferences.path === file.path ? preferences.scrollTop : 0;
    if (preferences.path !== file.path) patchPreferences({ path: file.path, search: "", scrollTop: 0 });
  }, [file?.path]);
  useLayoutEffect(() => {
    if (currentMatch >= 0) viewport.current?.querySelector(`[data-line="${currentMatch}"]`)?.scrollIntoView({ block: "nearest" });
  }, [search, currentMatch]);
  const back = () => { void run("back", () => controller.backInspector(), ""); };
  if (!file || (inspector?.path && file.path !== inspector.path)) return html`<div class="support-config"><button onClick=${back}>Back</button><${Feedback} state=${feedback.back} /><${LoadState} error=${data?.inspectorErrors?.file} label="Loading current file…" /></div>`;
  const lines = String(file.content ?? "").split("\n");
  const matchSet = new Set(matches);
  return html`<div class="support-config support-file">
    <div class="support-toolbar"><button onClick=${back}>Back</button><strong>Current file on disk</strong><${Feedback} state=${feedback.back} /></div>
    <h2 class="support-file__path">${file.path}</h2>
    <p class="support-scope">Read from disk when opened. This is not a historical tool result and does not update live.</p>
    <div class="support-toolbar"><input type="search" aria-label="Find in current file" placeholder="Find in file" value=${search} onInput=${(event) => { setMatchIndex(0); patchPreferences({ search: event.currentTarget.value, path: file.path }); }} />
      <span role="status">${search ? matches.length ? `${matchIndex % matches.length + 1} / ${matches.length} matching lines` : "No matches" : `${lines.length} lines`}</span>
      <button aria-label="Previous matching line" disabled=${!matches.length} onClick=${() => setMatchIndex((value) => (value - 1 + matches.length) % matches.length)}>Previous</button>
      <button aria-label="Next matching line" disabled=${!matches.length} onClick=${() => setMatchIndex((value) => (value + 1) % matches.length)}>Next</button>
      <button aria-pressed=${preferences.wrap} onClick=${() => patchPreferences({ wrap: !preferences.wrap })}>Wrap ${preferences.wrap ? "on" : "off"}</button>
      <button disabled=${feedback.copy?.pending} onClick=${() => { void run("copy", async () => {
        if (!globalThis.navigator?.clipboard?.writeText) throw new Error("Clipboard unavailable. Select file text to copy manually.");
        await navigator.clipboard.writeText(String(file.content ?? ""));
      }, "Copied"); }}>Copy file</button><${Feedback} state=${feedback.copy} pendingLabel="Copying…" />
    </div>
    <div ref=${viewport} class=${`support-file__content ${preferences.wrap ? "is-wrapped" : ""}`} tabindex="0" aria-label="Current file contents" onScroll=${(event) => patchPreferences({ scrollTop: event.currentTarget.scrollTop })}>
      ${file.content === "" ? html`<p class="support-empty">This file is empty.</p>` : lines.map((line, index) => html`<div key=${index} data-line=${index} class=${`support-file__line ${matchSet.has(index) ? "is-match" : ""} ${currentMatch === index ? "is-current-match" : ""}`}><span class="support-file__number" aria-hidden="true">${index + 1}</span><code>${line || "\n"}</code></div>`)}
    </div>
  </div>`;
}
