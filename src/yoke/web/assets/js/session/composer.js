import { html, useEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";

const MAX_PROMPT_ATTACHMENTS = 20;

export function SessionComposer({ sessionID, session, runtime, data, attentionCount = 0 }) {
  const capabilities = useStore((state) => state.capabilities);
  const connected = useStore((state) => state.connection.current);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef(null);

  useEffect(() => {
    setText("");
    setAttachments([]);
  }, [sessionID]);
  useEffect(() => {
    if (!data?.editorHandoff) return;
    setText(data.editorHandoff);
    controller.clearEditorHandoff(sessionID);
  }, [data?.editorHandoff, sessionID]);

  const hasContent = Boolean(text.trim() || attachments.length);
  const running = runtime?.state === "running" || runtime?.state === "stopping" || runtime?.state === "waiting_input";
  const canSteer = Boolean(capabilities?.features?.steering);
  const submit = async (delivery) => {
    if (!hasContent || busy || !connected) return;
    setBusy(true);
    try {
      await controller.submitPrompt(sessionID, { text, attachments, delivery });
      setText("");
      setAttachments([]);
    } catch (error) {
      controller.notice(error?.message || String(error));
    } finally {
      setBusy(false);
    }
  };
  const onKeyDown = (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    void submit(running && !canSteer ? "queue" : "steer");
  };
  const addFiles = async (files) => {
    const images = acceptedImageFiles(files, attachments.length);
    if (!images.length) return;
    for (const file of images) {
      try {
        const upload = await controller.upload(file, sessionID);
        setAttachments((current) => [...current, upload]);
      } catch (error) {
        controller.notice(error?.message || String(error));
      }
    }
  };
  const imageInput = useImageAttachmentInput({
    enabled: Boolean(capabilities?.features?.images && connected),
    addFiles,
  });

  return html`<div class="composer-region">
    <${SelectionControls} directory=${session.location.directory} selection=${session.selection} sessionID=${sessionID} disabled=${!connected || running} />
    ${attentionCount ? html`<div class="composer-attention-note">Resolve the required ${attentionCount === 1 ? "action" : "actions"} above. You can still queue a follow-up here.</div>` : null}
    <div
      class=${`composer-shell ${imageInput.dragActive ? "is-image-drag-active" : ""}`}
      onDragEnter=${imageInput.onDragEnter}
      onDragOver=${imageInput.onDragOver}
      onDragLeave=${imageInput.onDragLeave}
      onDrop=${imageInput.onDrop}
    >
      ${imageInput.dragActive ? html`<div class="composer-drop-hint" aria-hidden="true"><span>Drop images to attach</span></div>` : null}
      ${attachments.length ? html`<div class="composer-attachments">${attachments.map((attachment, index) => html`
        <span class="attachment-chip">▧ ${attachment.name}<button aria-label=${`Remove ${attachment.name}`} onClick=${() => setAttachments((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button></span>
      `)}</div>` : null}
      <textarea
        class="composer-input"
        rows="3"
        value=${text}
        aria-label="Prompt"
        placeholder=${connected ? "Ask Yoke to work…" : "Reconnect to send work"}
        disabled=${!connected}
        onInput=${(event) => setText(event.currentTarget.value)}
        onKeyDown=${onKeyDown}
        onPaste=${imageInput.onPaste}
      ></textarea>
      <div class="composer-footer">
        <div class="composer-footer__left">
          ${capabilities?.features?.images ? html`<button class="quiet-button" type="button" disabled=${!connected} onClick=${() => fileInput.current?.click()}>＋ Image</button>` : null}
          <input ref=${fileInput} class="visually-hidden" type="file" accept="image/*" multiple onChange=${(event) => { void addFiles([...event.currentTarget.files]); event.currentTarget.value = ""; }} />
          ${runtime?.state && runtime.state !== "idle" ? html`<span class="composer-runtime">${runtimeLabel(runtime)}</span>` : null}
        </div>
        <div class="composer-actions">
          ${running ? html`
            ${runtime?.state !== "stopping" ? html`<button class="quiet-button" disabled=${!connected} onClick=${() => controller.interrupt(sessionID).catch((error) => controller.notice(error?.message || String(error)))}>Interrupt</button>` : null}
            ${canSteer ? html`<button class="primary" disabled=${!hasContent || busy || !connected} onClick=${() => submit("steer")}>Steer now</button>` : null}
            <button class=${canSteer ? "secondary-action" : "primary"} disabled=${!hasContent || busy || !connected} onClick=${() => submit("queue")}>Queue next</button>
          ` : html`<button class="primary" disabled=${!hasContent || busy || !connected} onClick=${() => submit("steer")}>${busy ? "Sending…" : "Send"}</button>`}
        </div>
      </div>
    </div>
  </div>`;
}

export function DraftComposer({ draftID, draft }) {
  const connected = useStore((state) => state.connection.current);
  const capabilities = useStore((state) => state.capabilities);
  const recentLocations = useStore((state) => state.recentLocations);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef(null);
  const value = draft || { text: "", location: recentLocations[0]?.directory || "", attachments: [] };
  const update = (patch) => controller.updateDraft(draftID, patch);
  const submit = async () => {
    if (busy || !connected) return;
    setBusy(true);
    try { await controller.submitDraft(draftID); }
    catch (error) { controller.notice(error?.message || String(error)); }
    finally { setBusy(false); }
  };
  const addFiles = async (files) => {
    const images = acceptedImageFiles(files, value.attachments?.length || 0);
    if (!images.length) return;
    const next = [...(value.attachments || [])];
    for (const file of images) {
      try { next.push(await controller.upload(file, null)); }
      catch (error) { controller.notice(error?.message || String(error)); }
    }
    update({ attachments: next });
  };
  const imageInput = useImageAttachmentInput({
    enabled: Boolean(capabilities?.features?.images && connected),
    addFiles,
  });
  return html`<div class="draft-composer-wrap">
    <div class="draft-hero"><div class="draft-hero__eyebrow">New session</div><h1>What should Yoke work on?</h1><p>The session is created only when you send.</p></div>
    <div class="draft-location">
      <label>Working location<input list="recent-locations" value=${value.location || ""} placeholder="/path/to/project" onInput=${(event) => update({ location: event.currentTarget.value })} /></label>
      <datalist id="recent-locations">${recentLocations.map((item) => html`<option value=${item.directory}></option>`)}</datalist>
    </div>
    <${SelectionControls} directory=${value.location} selection=${{ provider: value.provider, model: value.model, reasoningEffort: value.reasoningEffort }} onDraftChange=${(selection) => update(selection)} />
    <div
      class=${`composer-shell composer-shell--draft ${imageInput.dragActive ? "is-image-drag-active" : ""}`}
      onDragEnter=${imageInput.onDragEnter}
      onDragOver=${imageInput.onDragOver}
      onDragLeave=${imageInput.onDragLeave}
      onDrop=${imageInput.onDrop}
    >
      ${imageInput.dragActive ? html`<div class="composer-drop-hint" aria-hidden="true"><span>Drop images to attach</span></div>` : null}
      ${value.attachments?.length ? html`<div class="composer-attachments">${value.attachments.map((attachment, index) => html`
        <span class="attachment-chip">▧ ${attachment.name}<button aria-label=${`Remove ${attachment.name}`} onClick=${() => update({ attachments: value.attachments.filter((_, itemIndex) => itemIndex !== index) })}>×</button></span>
      `)}</div>` : null}
      <textarea class="composer-input composer-input--draft" rows="8" autofocus value=${value.text || ""} placeholder="Describe the task…" onInput=${(event) => update({ text: event.currentTarget.value })} onPaste=${imageInput.onPaste} onKeyDown=${(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void submit(); } }}></textarea>
      <div class="composer-footer">
        <div class="composer-footer__left">
          ${capabilities?.features?.images ? html`<button class="quiet-button" disabled=${!connected} onClick=${() => fileInput.current?.click()}>＋ Image</button>` : null}
          <input ref=${fileInput} class="visually-hidden" type="file" accept="image/*" multiple onChange=${(event) => { void addFiles([...event.currentTarget.files]); event.currentTarget.value = ""; }} />
          <span class="muted tiny">⌘↵ send</span>
        </div>
        <button class="primary" disabled=${busy || !connected || (!(value.text || "").trim() && !value.attachments?.length) || !value.location} onClick=${submit}>${busy ? "Starting…" : "Start session"}</button>
      </div>
    </div>
  </div>`;
}

function useImageAttachmentInput({ enabled, addFiles }) {
  const [dragActive, setDragActive] = useState(false);
  const dragDepth = useRef(0);

  const onPaste = (event) => {
    const files = [...(event.clipboardData?.files || [])];
    const images = files.filter(isImageFile);
    if (!images.length) return;
    event.preventDefault();
    if (!enabled) {
      controller.notice("Image attachments are unavailable for this daemon.");
      return;
    }
    void addFiles(images);
  };
  const onDragEnter = (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragActive(true);
  };
  const onDragOver = (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  };
  const onDragLeave = (event) => {
    if (dragDepth.current === 0) return;
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  };
  const onDrop = (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDragActive(false);
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.some(isImageFile)) {
      if (files.length) controller.notice("Only image files can be attached to a prompt.");
      return;
    }
    if (!enabled) {
      controller.notice("Image attachments are unavailable for this daemon.");
      return;
    }
    void addFiles(files);
  };
  return { dragActive, onPaste, onDragEnter, onDragOver, onDragLeave, onDrop };
}

function hasFileTransfer(dataTransfer) {
  return [...(dataTransfer?.types || [])].includes("Files") || Boolean(dataTransfer?.files?.length);
}

function isImageFile(file) {
  return file?.type?.startsWith("image/") || /\.(?:gif|heic|heif|jpe?g|png|webp)$/i.test(file?.name || "");
}

function acceptedImageFiles(files, existingCount) {
  const input = [...files];
  const images = input.filter(isImageFile);
  if (images.length < input.length) controller.notice("Only image files can be attached to a prompt.");
  const accepted = images.slice(0, Math.max(0, MAX_PROMPT_ATTACHMENTS - existingCount));
  if (accepted.length < images.length) controller.notice(`A prompt can contain up to ${MAX_PROMPT_ATTACHMENTS} images.`);
  return accepted;
}

function SelectionControls({ directory, selection, sessionID = null, onDraftChange = null, disabled = false }) {
  const bootstrapProviders = useStore((state) => state.providers);
  const providerCatalog = useStore((state) => state.providerCatalogs?.[directory || ""] || null);
  const modelsMap = useStore((state) => state.models || {});
  const [provider, setProvider] = useState(selection?.provider || "");
  const [model, setModel] = useState(selection?.model || "");
  const [effort, setEffort] = useState(selection?.reasoningEffort || "");
  const providers = providerCatalog || bootstrapProviders;
  const modelKey = `${directory || ""}:${provider || ""}:`;
  const models = modelsMap[modelKey] || [];
  const selectedModel = models.find((item) => item.id === model);

  useEffect(() => {
    setProvider(selection?.provider || "");
    setModel(selection?.model || "");
    setEffort(selection?.reasoningEffort || "");
  }, [selection?.provider, selection?.model, selection?.reasoningEffort, sessionID]);
  useEffect(() => { if (directory) void controller.loadProviders(directory); }, [directory]);
  useEffect(() => { if (directory && provider) void controller.loadModels(directory, provider); }, [directory, provider]);

  const applyDraft = (next) => onDraftChange?.({ provider: next.provider, model: next.model, reasoningEffort: next.effort });
  const selectProvider = (nextProvider) => {
    const info = providers.find((item) => item.id === nextProvider);
    const nextModel = info?.currentModel || "";
    const nextEffort = info?.currentReasoningEffort || "";
    setProvider(nextProvider); setModel(nextModel); setEffort(nextEffort);
    if (sessionID && nextProvider && nextModel) void controller.setSelection(sessionID, nextProvider, nextModel, nextEffort);
    else applyDraft({ provider: nextProvider, model: nextModel, effort: nextEffort });
  };
  const selectModel = (nextModel) => {
    const info = models.find((item) => item.id === nextModel);
    const nextEffort = info?.reasoningEfforts?.includes(effort) ? effort : info?.reasoningEfforts?.[0] || "";
    setModel(nextModel); setEffort(nextEffort);
    if (sessionID && provider && nextModel) void controller.setSelection(sessionID, provider, nextModel, nextEffort);
    else applyDraft({ provider, model: nextModel, effort: nextEffort });
  };
  const selectEffort = (nextEffort) => {
    setEffort(nextEffort);
    if (sessionID && provider && model) void controller.setSelection(sessionID, provider, model, nextEffort);
    else applyDraft({ provider, model, effort: nextEffort });
  };
  return html`<div class="selection-controls" aria-label="Model selection">
    <select value=${provider} disabled=${disabled} aria-label="Provider" onChange=${(event) => selectProvider(event.currentTarget.value)}>
      <option value="">Provider</option>${providers.map((item) => html`<option value=${item.id} disabled=${!item.ready}>${item.id}${item.ready ? "" : " · unavailable"}</option>`)}
    </select>
    <select value=${model} disabled=${disabled || !provider} aria-label="Model" onChange=${(event) => selectModel(event.currentTarget.value)}>
      <option value="">Model</option>${models.map((item) => html`<option value=${item.id}>${item.name || item.id}</option>`)}
    </select>
    <select value=${effort} disabled=${disabled || !selectedModel?.reasoningEfforts?.length} aria-label="Reasoning effort" onChange=${(event) => selectEffort(event.currentTarget.value)}>
      <option value="">Effort</option>${(selectedModel?.reasoningEfforts || []).map((value) => html`<option value=${value}>${value}</option>`)}
    </select>
  </div>`;
}

function runtimeLabel(runtime) {
  if (runtime.state === "waiting_input") return "Waiting for input";
  if (runtime.state === "stopping") return "Stopping";
  if (runtime.state === "error") return "Error";
  return "Working";
}
