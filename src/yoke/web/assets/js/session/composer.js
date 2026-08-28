import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import {
  applySlashCompletion,
  handleSlashMenuKey,
  SlashCompletionMenu,
  useSlashCompletions,
} from "./slash-menu.js";

const MAX_PROMPT_ATTACHMENTS = 20;

export function SessionComposer({ sessionID, session, runtime, data, attentionCount = 0 }) {
  const capabilities = useStore((state) => state.capabilities);
  const connected = useStore((state) => state.connection.current);
  const overlayOpen = useStore((state) => Boolean(state.ui.inspector || state.ui.commandPaletteOpen));
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const fileInput = useRef(null);
  const promptInput = useRef(null);
  const escapePrefixAt = useRef(0);

  useEffect(() => {
    setText("");
    setAttachments([]);
    setExpanded(false);
  }, [sessionID]);
  useEffect(() => {
    if (!data?.editorHandoff) return;
    setText(data.editorHandoff);
    controller.clearEditorHandoff(sessionID);
  }, [data?.editorHandoff, sessionID]);
  useLayoutEffect(() => {
    resizeComposerInput(promptInput.current);
  }, [text, expanded]);
  useEffect(() => {
    const resize = () => resizeComposerInput(promptInput.current);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  const hasContent = Boolean(text.trim() || attachments.length);
  const running = runtime?.state === "running" || runtime?.state === "stopping" || runtime?.state === "waiting_input";
  const canSteer = Boolean(capabilities?.features?.steering);
  const slashMenu = useSlashCompletions({
    text,
    enabled: !attachments.length,
    sessionID,
    directory: session.location.directory,
    hasSession: true,
  });
  const executeSlash = async (value) => {
    const result = await controller.runSlashCommand(value, {
      sessionID,
      directory: session.location.directory,
    });
    if (!result.handled) return false;
    slashMenu.close();
    if (result.action === "image") fileInput.current?.click();
    if (result.clear !== false) setText("");
    return true;
  };
  const chooseSlash = (item, { submit: shouldSubmit = false } = {}) => {
    const completion = applySlashCompletion(text, item, {
      appendSpace: !shouldSubmit && item.kind !== "command",
    });
    setText(completion.text);
    slashMenu.close();
    requestAnimationFrame(() => {
      promptInput.current?.focus();
      promptInput.current?.setSelectionRange(completion.cursor, completion.cursor);
    });
    if (shouldSubmit) void executeSlash(completion.text).catch((error) => controller.notice(error?.message || String(error)));
  };
  const submit = async (delivery) => {
    if (!hasContent || busy || !connected) return;
    const submittedText = text;
    const submittedAttachments = [...attachments];
    setBusy(true);
    try {
      if (!attachments.length && await executeSlash(text)) return;
      // Hand ownership to the optimistic transcript before the network round
      // trip. The composer must never show the same prompt at the same time as
      // the optimistic user row.
      setText("");
      setAttachments([]);
      await controller.submitPrompt(sessionID, {
        text: submittedText,
        attachments: submittedAttachments,
        delivery,
      });
    } catch (error) {
      // The input is read-only while admission is pending, so a failed send can
      // restore the exact draft without overwriting newer typing.
      setText(submittedText);
      setAttachments(submittedAttachments);
      controller.notice(error?.message || String(error));
    } finally {
      setBusy(false);
    }
  };
  const onKeyDown = (event) => {
    if (event.isComposing) return;
    if (busy) return;
    if (handleSlashMenuKey(event, slashMenu, chooseSlash)) return;
    const key = event.key.toLowerCase();
    if (event.key === "Escape") {
      if (overlayOpen) return;
      escapePrefixAt.current = performance.now();
      return;
    }
    if (event.ctrlKey && !event.metaKey && key === "j") {
      event.preventDefault();
      insertTextareaText(event.currentTarget, "\n", setText);
      return;
    }
    if (event.ctrlKey && !event.metaKey && key === "u") {
      event.preventDefault();
      if (attachments.length) setAttachments((current) => current.slice(0, -1));
      return;
    }
    if (event.key === "Tab" && event.shiftKey) {
      event.preventDefault();
      void controller.cycleReasoningEffort();
      return;
    }
    if (event.key === "Enter" && performance.now() - escapePrefixAt.current <= 650) {
      escapePrefixAt.current = 0;
      event.preventDefault();
      insertTextareaText(event.currentTarget, "\n", setText);
      return;
    }
    escapePrefixAt.current = 0;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit(running && !canSteer ? "queue" : "steer");
      return;
    }
    if (event.key === "Tab" && !event.shiftKey && running && hasContent) {
      event.preventDefault();
      void submit("queue");
    }
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
    <div class="composer-meta-row">
      <${SelectionControls} directory=${session.location.directory} selection=${session.selection} sessionID=${sessionID} disabled=${!connected || running} />
      <${ContextWindowUsage} sessionID=${sessionID} directory=${session.location.directory} selection=${session.selection} usage=${data?.contextUsage ?? session.contextUsage} />
    </div>
    ${attentionCount ? html`<div class="composer-attention-note">Resolve the required ${attentionCount === 1 ? "action" : "actions"} above. You can still queue a follow-up here.</div>` : null}
    <div class="composer-shell-wrap">
      <${SlashCompletionMenu} items=${slashMenu.items} activeIndex=${slashMenu.activeIndex} loading=${slashMenu.loading} onChoose=${chooseSlash} />
      <div
        class=${`composer-shell ${imageInput.dragActive ? "is-image-drag-active" : ""}`}
        onDragEnter=${imageInput.onDragEnter}
        onDragOver=${imageInput.onDragOver}
        onDragLeave=${imageInput.onDragLeave}
        onDrop=${imageInput.onDrop}
      >
      ${imageInput.dragActive ? html`<div class="composer-drop-hint" aria-hidden="true"><span>Drop images to attach</span></div>` : null}
      <button
        class="composer-resize-button"
        type="button"
        aria-pressed=${expanded}
        aria-label=${expanded ? "Use compact prompt editor" : "Use larger prompt editor"}
        title=${expanded ? "Compact prompt editor" : "Expand prompt editor"}
        onPointerDown=${(event) => event.preventDefault()}
        onClick=${() => {
          setExpanded((value) => !value);
          requestAnimationFrame(() => promptInput.current?.focus());
        }}
      ><span aria-hidden="true">${expanded ? "↙" : "↗"}</span><span>${expanded ? "Compact" : "Expand"}</span></button>
      ${attachments.length ? html`<div class="composer-attachments">${attachments.map((attachment, index) => html`
        <span class="attachment-chip">▧ ${attachment.name}<button aria-label=${`Remove ${attachment.name}`} onClick=${() => setAttachments((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button></span>
      `)}</div>` : null}
      <textarea
        ref=${promptInput}
        class=${`composer-input ${expanded ? "is-expanded" : ""}`}
        rows="1"
        value=${text}
        aria-label="Prompt"
        aria-autocomplete="list"
        aria-controls=${slashMenu.items.length || slashMenu.loading ? "slash-completion-menu" : undefined}
        aria-expanded=${Boolean(slashMenu.items.length || slashMenu.loading)}
        aria-activedescendant=${slashMenu.items[slashMenu.activeIndex] ? `slash-completion-menu-option-${slashMenu.activeIndex}` : undefined}
        placeholder=${connected ? "Ask Yoke to work…" : "Reconnect to send work"}
        disabled=${!connected}
        readOnly=${busy}
        aria-busy=${busy}
        onInput=${(event) => setText(event.currentTarget.value)}
        onKeyDown=${onKeyDown}
        onPaste=${imageInput.onPaste}
      ></textarea>
      <div class="composer-footer">
        <div class="composer-footer__left">
          ${capabilities?.features?.images ? html`<button class="quiet-button" type="button" disabled=${!connected || busy} onClick=${() => fileInput.current?.click()}>＋ Image</button>` : null}
          <input ref=${fileInput} class="visually-hidden" type="file" accept="image/*" multiple onChange=${(event) => { void addFiles([...event.currentTarget.files]); event.currentTarget.value = ""; }} />
        </div>
        <div class="composer-actions">
          ${running ? html`
            ${runtime?.state !== "stopping" ? html`
              <button
                class="composer-icon-action composer-stop-button"
                aria-label="Stop current turn"
                title="Stop current turn · Esc Esc"
                disabled=${!connected}
                onClick=${() => controller.interrupt(sessionID).catch((error) => controller.notice(error?.message || String(error)))}
              ><span aria-hidden="true" class="composer-stop-button__glyph"></span></button>
            ` : null}
            ${canSteer ? html`
              <button
                class="primary composer-icon-action composer-send-button"
                aria-label="Steer now"
                title="Steer now · Enter"
                disabled=${!hasContent || busy || !connected}
                onClick=${() => submit("steer")}
              ><${SendArrow} /></button>
            ` : null}
            <button
              class=${canSteer ? "secondary-action composer-queue-button" : "primary composer-queue-button"}
              aria-label="Queue message"
              title="Queue message · Tab"
              disabled=${!hasContent || busy || !connected}
              onClick=${() => submit("queue")}
            >Queue Msg</button>
          ` : html`
            <button
              class="primary composer-icon-action composer-send-button"
              aria-label=${busy ? "Sending message" : "Send message"}
              title="Send · Enter"
              disabled=${!hasContent || busy || !connected}
              onClick=${() => submit("steer")}
            >${busy ? html`<span class="pending-spinner" aria-hidden="true"></span>` : html`<${SendArrow} />`}</button>
          `}
        </div>
      </div>
    </div>
    </div>
  </div>`;
}

function resizeComposerInput(input) {
  if (!input) return;
  input.style.height = "auto";
  const styles = window.getComputedStyle(input);
  const minHeight = Number.parseFloat(styles.minHeight) || 0;
  const parsedMaxHeight = Number.parseFloat(styles.maxHeight);
  const maxHeight = Number.isFinite(parsedMaxHeight) ? parsedMaxHeight : input.scrollHeight;
  input.style.height = `${Math.ceil(Math.max(minHeight, Math.min(input.scrollHeight, maxHeight)))}px`;
  input.style.overflowY = input.scrollHeight > maxHeight + 1 ? "auto" : "hidden";
}

export function DraftComposer({ draftID, draft }) {
  const connected = useStore((state) => state.connection.current);
  const capabilities = useStore((state) => state.capabilities);
  const recentLocations = useStore((state) => state.recentLocations);
  const overlayOpen = useStore((state) => Boolean(state.ui.inspector || state.ui.commandPaletteOpen));
  const [busy, setBusy] = useState(false);
  const fileInput = useRef(null);
  const promptInput = useRef(null);
  const escapePrefixAt = useRef(0);
  const value = draft || { text: "", location: recentLocations[0]?.directory || "", attachments: [] };
  const update = (patch) => controller.updateDraft(draftID, patch);
  const slashMenu = useSlashCompletions({
    text: value.text || "",
    enabled: !(value.attachments || []).length,
    directory: value.location || "",
    hasSession: false,
  });
  const executeSlash = async (text) => {
    const result = await controller.runSlashCommand(text, {
      draftID,
      directory: value.location || "",
    });
    if (!result.handled) return false;
    slashMenu.close();
    if (result.action === "image") fileInput.current?.click();
    if (result.clear !== false) update({ text: "" });
    return true;
  };
  const chooseSlash = (item, { submit: shouldSubmit = false } = {}) => {
    const completion = applySlashCompletion(value.text || "", item, {
      appendSpace: !shouldSubmit && item.kind !== "command",
    });
    update({ text: completion.text });
    slashMenu.close();
    requestAnimationFrame(() => {
      promptInput.current?.focus();
      promptInput.current?.setSelectionRange(completion.cursor, completion.cursor);
    });
    if (shouldSubmit) void executeSlash(completion.text).catch((error) => controller.notice(error?.message || String(error)));
  };
  const submit = async () => {
    if (busy || !connected) return;
    setBusy(true);
    try {
      if (!(value.attachments || []).length && await executeSlash(value.text || "")) return;
      await controller.submitDraft(draftID);
    }
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
    <div class="composer-shell-wrap">
      <${SlashCompletionMenu} items=${slashMenu.items} activeIndex=${slashMenu.activeIndex} loading=${slashMenu.loading} onChoose=${chooseSlash} />
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
      <textarea ref=${promptInput} class="composer-input composer-input--draft" rows="8" autofocus value=${value.text || ""} placeholder="Describe the task…" aria-autocomplete="list" aria-controls=${slashMenu.items.length || slashMenu.loading ? "slash-completion-menu" : undefined} aria-expanded=${Boolean(slashMenu.items.length || slashMenu.loading)} aria-activedescendant=${slashMenu.items[slashMenu.activeIndex] ? `slash-completion-menu-option-${slashMenu.activeIndex}` : undefined} onInput=${(event) => update({ text: event.currentTarget.value })} onPaste=${imageInput.onPaste} onKeyDown=${(event) => {
        if (event.isComposing) return;
        if (handleSlashMenuKey(event, slashMenu, chooseSlash)) return;
        const key = event.key.toLowerCase();
        if (event.key === "Escape") {
          if (overlayOpen) return;
          escapePrefixAt.current = performance.now();
          return;
        }
        if (event.ctrlKey && !event.metaKey && key === "j") {
          event.preventDefault();
          insertTextareaText(event.currentTarget, "\n", (text) => update({ text }));
          return;
        }
        if (event.ctrlKey && !event.metaKey && key === "u") {
          event.preventDefault();
          if (value.attachments?.length) update({ attachments: value.attachments.slice(0, -1) });
          return;
        }
        if (event.key === "Tab" && event.shiftKey) {
          event.preventDefault();
          void controller.cycleReasoningEffort();
          return;
        }
        if (event.key === "Enter" && performance.now() - escapePrefixAt.current <= 650) {
          escapePrefixAt.current = 0;
          event.preventDefault();
          insertTextareaText(event.currentTarget, "\n", (text) => update({ text }));
          return;
        }
        escapePrefixAt.current = 0;
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          void submit();
        }
      }}></textarea>
      <div class="composer-footer">
        <div class="composer-footer__left">
          ${capabilities?.features?.images ? html`<button class="quiet-button" disabled=${!connected} onClick=${() => fileInput.current?.click()}>＋ Image</button>` : null}
          <input ref=${fileInput} class="visually-hidden" type="file" accept="image/*" multiple onChange=${(event) => { void addFiles([...event.currentTarget.files]); event.currentTarget.value = ""; }} />
          <span class="muted tiny">↵ send · ⇧↵ newline</span>
        </div>
        <button
          class="primary composer-icon-action composer-send-button"
          aria-label=${busy ? "Starting session" : "Start session"}
          title=${busy ? "Starting session" : "Start session"}
          disabled=${busy || !connected || (!(value.text || "").trim() && !value.attachments?.length) || !value.location}
          onClick=${submit}
        ><${SendArrow} /></button>
      </div>
    </div>
    </div>
  </div>`;
}

function SendArrow() {
  return html`<svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20">
    <path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"></path>
  </svg>`;
}

function insertTextareaText(textarea, insertion, apply) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? start;
  const next = `${textarea.value.slice(0, start)}${insertion}${textarea.value.slice(end)}`;
  const cursor = start + insertion.length;
  apply(next);
  requestAnimationFrame(() => {
    textarea.focus();
    textarea.setSelectionRange(cursor, cursor);
  });
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

function ContextWindowUsage({ sessionID, directory, selection, usage }) {
  const modelsMap = useStore((state) => state.models || {});
  const modelKey = `${directory || ""}:${selection?.provider || ""}:`;
  const selectedModel = (modelsMap[modelKey] || []).find((item) => item.id === selection?.model);
  const inputTokens = firstInteger(usage?.input_tokens, usage?.inputTokens);
  const maxTokens = firstInteger(
    usage?.max_total_tokens,
    usage?.maxTotalTokens,
    selectedModel?.contextWindowTokens,
  );
  let usagePercent = firstInteger(usage?.usage_percent, usage?.usagePercent);
  if (usagePercent == null && inputTokens != null && maxTokens) {
    usagePercent = Math.round((inputTokens / maxTokens) * 100);
  }
  if (usagePercent != null) usagePercent = Math.min(100, Math.max(0, usagePercent));
  const remainingPercent = usagePercent == null ? null : Math.max(0, 100 - usagePercent);
  const remainingTokens = inputTokens != null && maxTokens != null ? Math.max(0, maxTokens - inputTokens) : null;
  const className = usagePercent == null
    ? "context-usage is-unknown"
    : usagePercent >= 90
      ? "context-usage is-critical"
      : usagePercent >= 75
        ? "context-usage is-warning"
        : "context-usage";
  const tooltipID = `context-usage-${sessionID}`;
  const angle = `${(usagePercent || 0) * 3.6}deg`;
  const measured = inputTokens != null && maxTokens != null;
  return html`<span
    class=${className}
    role="img"
    tabindex="0"
    aria-describedby=${tooltipID}
    aria-label=${usagePercent == null ? "Context window usage unavailable" : `Context window ${usagePercent}% used`}
  >
    <span class="context-usage__ring" style=${{ "--context-usage-angle": angle }} aria-hidden="true"></span>
    <span>${usagePercent == null ? "—" : `${usagePercent}%`}</span>
    <span id=${tooltipID} class="context-usage__tooltip" role="tooltip">
      ${measured
        ? html`<strong>${formatTokenCount(inputTokens)} / ${formatTokenCount(maxTokens)} tokens · ${usagePercent}% used</strong><br />${remainingPercent}% of the model context window remains (${formatTokenCount(remainingTokens)} tokens). This is the latest provider request's model-visible input, including instructions, compacted memory, conversation history, and tool context. Yoke reserves output headroom and can compact before the raw model limit is reached.`
        : html`<strong>Context usage not measured yet.</strong><br />Yoke updates this ring after it measures or receives usage for a provider request.${maxTokens ? ` The selected model has a ${formatTokenCount(maxTokens)} token context window.` : ""}`}
    </span>
  </span>`;
}

function firstInteger(...values) {
  return values.find((value) => Number.isInteger(value)) ?? null;
}

function formatTokenCount(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}k`;
  return String(value);
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
