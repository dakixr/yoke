import { html, useLayoutEffect, useRef } from "../../../vendor/htm-preact.js";
import { outputSummary, outputText } from "./logic.js";

export function ProcessOutput({ process, preferences, patchPreferences }) {
  const ref = useRef(null);
  const saved = preferences.outputs?.[process.processID] || { following: true, scrollTop: 0 };
  const following = saved.following !== false;
  const followRef = useRef(following);
  followRef.current = following;
  const save = (patch) => patchPreferences((current) => ({ outputs: {
    ...current.outputs, [process.processID]: { ...current.outputs?.[process.processID], ...patch },
  } }));
  useLayoutEffect(() => {
    if (ref.current) ref.current.scrollTop = following ? ref.current.scrollHeight : saved.scrollTop || 0;
  }, [process.processID]);
  useLayoutEffect(() => {
    if (ref.current && followRef.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [process.output?.tail, preferences.wrap, following]);
  return html`<section class="process-terminal">
    <div class="process-terminal__header"><div><strong>Output</strong><span>${outputSummary(process.output)}</span><span>${process.status === "running" ? following ? "Following live output" : "Auto-follow paused while reading" : "Final retained output"}</span></div>
      <button aria-pressed=${preferences.wrap} onClick=${() => patchPreferences({ wrap: !preferences.wrap })}>Wrap ${preferences.wrap ? "on" : "off"}</button>
    </div>
    <div class="process-terminal__viewport"><pre ref=${ref} tabindex="0" aria-label="Process output" class=${`process-output ${preferences.wrap ? "is-wrapped" : ""}`} onScroll=${(event) => {
      const node = event.currentTarget;
      const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 28;
      followRef.current = atBottom;
      save({ following: atBottom, scrollTop: node.scrollTop });
    }}>${outputText(process)}</pre>
      ${!following ? html`<button class="process-jump-live" onClick=${() => {
        followRef.current = true;
        if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
        save({ following: true, scrollTop: ref.current?.scrollTop || 0 });
      }}>${process.status === "running" ? "Jump to live" : "Jump to end"}</button>` : null}
    </div>
  </section>`;
}
