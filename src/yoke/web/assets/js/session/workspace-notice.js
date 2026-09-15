import { html, useEffect, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import { workspaceUnavailable } from "../state/workspace/status.js";
import { LocationPicker } from "./location-picker.js";

export function WorkspaceNotice({ session, runtime }) {
  const connected = useStore((state) => state.connection.current);
  const recentLocations = useStore((state) => state.recentLocations);
  const [choosing, setChoosing] = useState(false);
  const [directory, setDirectory] = useState("");
  const [expectedDirectory, setExpectedDirectory] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setChoosing(false);
    setDirectory("");
    setError("");
  }, [session.id]);
  const workspace = workspaceUnavailable(session, runtime);
  if (!workspace && !choosing) return null;
  const relocate = async () => {
    if (!directory || busy || !connected) return;
    setBusy(true);
    setError("");
    try {
      const result = await controller.workspace.relocate(session.id, directory, expectedDirectory);
      if (result) setChoosing(false);
    } catch (failure) {
      setError(failure?.message || String(failure));
      if (failure?.code === "session_workspace_conflict") {
        await controller.refreshSessionSummary(session.id).catch(() => {});
      }
    } finally { setBusy(false); }
  };
  return html`<section class="workspace-notice" aria-label="Session workspace">
    <div role="status"><strong>Workspace unavailable</strong>
      <p>${workspace?.message || "Choose the workspace for this session."}</p>
      <p><code>${session.location?.directory || "No workspace configured"}</code></p>
      <p>Your transcript and draft are kept. Restore this directory, then retry, or explicitly relocate this session to an existing folder.</p>
    </div>
    ${choosing ? html`<div class="workspace-notice__picker">
      <p>Relocation keeps this session ID and history. It does not move files. Stop live work and remove pending queue items first, including paused items.</p>
      <${LocationPicker} value=${directory} recentLocations=${recentLocations} onChange=${setDirectory} />
      <p>Selected folder: <code>${directory || "Choose a folder above"}</code></p>
      <button class="primary" disabled=${!directory || busy || !connected} onClick=${relocate}>${busy ? "Relocating…" : "Relocate this session"}</button>
      <button class="quiet-button" disabled=${busy} onClick=${() => setChoosing(false)}>Cancel</button>
      ${error ? html`<p role="alert">${error}</p>` : null}
    </div>` : html`<div>
      <button class="primary" disabled=${!connected} onClick=${() => {
        setExpectedDirectory(session.location?.directory);
        setChoosing(true);
        setError("");
      }}>Relocate workspace</button>
      <button class="quiet-button" disabled=${!connected} onClick=${() => controller.refreshSessionSummary(session.id).catch((failure) => controller.notice(failure.message))}>Retry workspace</button>
    </div>`}
  </section>`;
}
