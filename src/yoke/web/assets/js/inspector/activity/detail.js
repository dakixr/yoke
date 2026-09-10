import { html, useLayoutEffect, useRef } from "../../../vendor/htm-preact.js";
import { CopyButton } from "../tool-fields.js";
import { valueToText } from "../tool-logic.js";
import { callOutcome, formatDuration, formatTimestamp } from "./presenters.js";
import { callArgumentState, capturedOutput, projectedResultValue } from "./data.js";
import { CallSignature } from "./signature.js";
import { ToolValue } from "./value.js";

export function ActivityDetail({ detail, preferences, patchPreferences }) {
  const { wrap } = preferences;
  const nodeRef = useRef(null);
  const outcome = callOutcome(detail);
  const output = capturedOutput(detail);
  const running = detail.status === "running" || detail.status === "pending";
  const showCapturedOutput = Boolean(running && output);
  const args = callArgumentState(detail);
  const shownResult = detail.resultProjection ?? null;
  const projectedResult = projectedResultValue(shownResult);

  useLayoutEffect(() => {
    if (nodeRef.current) nodeRef.current.scrollTop = preferences.detailPositions?.[detail.id] || 0;
  }, [detail.id, preferences.pane]);

  return html`<article class="activity-detail" ref=${nodeRef} onScroll=${(event) => {
    const scrollTop = event.currentTarget.scrollTop;
    patchPreferences((previous) => ({ detailPositions: { ...previous.detailPositions, [detail.id]: scrollTop } }));
  }}>
    <header class="activity-detail__meta">
      <span class=${`activity-status activity-status--${outcome.tone}`}>${outcome.label}</span>
      <time dateTime=${detail.time?.started || null}>${formatTimestamp(detail.time?.started)}</time>
      <span>${formatDuration(detail.time?.durationMs)}</span>
      ${Number.isFinite(detail.sequence) ? html`<span>#${detail.sequence}</span>` : null}
      <code title=${detail.id}>${detail.id}</code>
      <button aria-pressed=${wrap} onClick=${() => patchPreferences({ wrap: !wrap })}>Wrap ${wrap ? "on" : "off"}</button>
    </header>

    <section class="activity-call-section">
      <div class="activity-section-head">
        <h2>Call</h2>
        <${CopyButton} value=${args.shown != null ? valueToText(args.shown) : args.raw ?? ""} label="Copy args" title="Copy displayed tool arguments" />
      </div>
      <${CallSignature} call=${detail} wrap=${wrap} />
    </section>

    <section class="activity-result-section">
      <div class="activity-section-head">
        <h2>Result</h2>
        ${shownResult != null || detail.result != null ? html`<${CopyButton} value=${shownResult ?? valueToText(detail.result)} label="Copy result" title="Copy result shown here" />` : null}
      </div>
      ${projectedResult?.kind === "json"
        ? html`<div class="activity-projected-json"><${ToolValue} value=${projectedResult.value} wrap=${wrap} /></div>`
        : projectedResult?.kind === "text"
          ? html`<pre class=${`activity-model-result ${wrap ? "is-wrapped" : ""}`}>${projectedResult.text}</pre>`
        : detail.result != null
          ? html`<${ToolValue} value=${detail.result} wrap=${wrap} />`
        : html`<p class="activity-result-empty">${running ? "Waiting for result…" : "No result was recorded."}</p>`}

      ${showCapturedOutput ? html`<div class="activity-captured-output">
        <div class="activity-subhead"><span>${running ? "Live output" : "Captured output"}</span><${CopyButton} value=${output} label="Copy" title="Copy captured output" /></div>
        <pre class=${wrap ? "is-wrapped" : ""}>${output}</pre>
      </div>` : null}
    </section>
  </article>`;
}
