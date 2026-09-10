import { html, useLayoutEffect, useRef, useState } from "../../../vendor/htm-preact.js";
import { controller } from "../../state/controller.js";
import { Feedback, useActions } from "../config/feedback.js";

export function ProcessControls({ process }) {
  const [stdin, setStdin] = useState("");
  const [confirming, setConfirming] = useState(false);
  const cancelRef = useRef(null);
  const terminateRef = useRef(null);
  const { feedback, run } = useActions();
  const pending = Object.values(feedback).some((state) => state.pending);
  useLayoutEffect(() => { if (confirming) cancelRef.current?.focus(); }, [confirming]);
  const cancel = () => { setConfirming(false); terminateRef.current?.focus(); };
  return html`<div class="process-controls">
    <label class="process-stdin"><span>Standard input</span><textarea rows="2" value=${stdin} disabled=${pending} placeholder="Text to send exactly as entered" onInput=${(event) => setStdin(event.currentTarget.value)} /></label>
    <div class="process-controls__input"><button class="primary" disabled=${!stdin || pending || confirming} onClick=${async () => {
      if (await run("input", () => controller.sendProcessInput(process.processID, stdin), "Input sent")) setStdin("");
    }}>Send input</button><span>No newline is added automatically.</span><${Feedback} state=${feedback.input} pendingLabel="Sending…" /></div>
    <div class="process-controls__signals"><div><button disabled=${pending || confirming} onClick=${() => { void run("interrupt", () => controller.signalProcess(process.processID, "interrupt"), "Interrupt sent"); }}>Interrupt</button><span>Request an interrupt, like Ctrl+C.</span><${Feedback} state=${feedback.interrupt} pendingLabel="Interrupting…" /></div>
      <div><button ref=${terminateRef} class="danger-text" disabled=${pending || confirming} onClick=${() => setConfirming(true)}>Terminate…</button><span>Stop the process.</span></div>
    </div>
    ${confirming ? html`<div class="process-confirm" role="group" aria-label="Confirm process termination" onKeyDown=${(event) => { if (event.key === "Escape") { event.stopPropagation(); event.preventDefault(); if (!pending) cancel(); } }}><p>Terminate process #${process.runtimeSessionID}? Unsaved work in this process may be lost.</p><button ref=${cancelRef} disabled=${pending} onClick=${cancel}>Cancel</button><button class="danger-text" disabled=${pending} onClick=${async () => {
      if (await run("terminate", () => controller.signalProcess(process.processID, "terminate"), "Termination sent")) setConfirming(false);
    }}>Terminate process</button></div>` : null}
    <${Feedback} state=${feedback.terminate} pendingLabel="Terminating…" />
  </div>`;
}
