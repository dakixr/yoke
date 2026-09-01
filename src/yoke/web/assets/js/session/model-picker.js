import { html, useEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useStore } from "../state/hooks.js";
import {
  filterModelChoices,
  formatContextWindow,
  groupModelChoices,
  modelSelectionErrorMessage,
  modelNavigationIndex,
  resolveModelEffort,
} from "./model-picker-logic.js";

export function ModelSelectionControl({ directory, selection, sessionID = null, onDraftChange = null, disabled = false }) {
  const bootstrapProviders = useStore((state) => state.providers);
  const providerCatalog = useStore((state) => state.providerCatalogs?.[directory || ""] || null);
  const modelsMap = useStore((state) => state.models || {});
  const [provider, setProvider] = useState(selection?.provider || "");
  const [model, setModel] = useState(selection?.model || "");
  const [effort, setEffort] = useState(selection?.reasoningEffort || "");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectionError, setSelectionError] = useState("");
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const searchRef = useRef(null);
  const resultsRef = useRef(null);
  const selectionGenerationRef = useRef(0);

  const providers = providerCatalog || bootstrapProviders;
  const selectedKey = `${directory || ""}:${provider || ""}:`;
  const allKey = `${directory || ""}::`;
  const providerModels = modelsMap[selectedKey] || [];
  const allModels = modelsMap[allKey] || [];
  const selectedModel = providerModels.find((item) => item.id === model)
    || allModels.find((item) => item.provider === provider && item.id === model)
    || null;
  const filteredModels = useMemo(
    () => filterModelChoices(allModels, query, providers),
    [allModels, providers, query],
  );
  const groups = useMemo(() => groupModelChoices(filteredModels), [filteredModels]);
  const selectableModels = filteredModels.filter((item) => item.providerReady);

  useEffect(() => {
    setProvider(selection?.provider || "");
    setModel(selection?.model || "");
    setEffort(selection?.reasoningEffort || "");
  }, [selection?.provider, selection?.model, selection?.reasoningEffort, sessionID]);

  useEffect(() => {
    selectionGenerationRef.current += 1;
    setSelectionError("");
  }, [sessionID]);

  useEffect(() => {
    if (!directory) return;
    void controller.loadProviders(directory).catch(() => {});
  }, [directory]);

  useEffect(() => {
    if (!directory || !provider) return;
    void controller.loadModels(directory, provider).catch(() => {});
  }, [directory, provider]);

  useEffect(() => {
    if (!open || !directory) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    void controller.loadModels(directory)
      .catch((loadError) => {
        if (!cancelled) setError(loadError?.message || String(loadError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [directory, open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => searchRef.current?.focus());
  }, [open]);

  useEffect(() => {
    if (!open || query) return;
    const currentIndex = selectableModels.findIndex((item) => item.provider === provider && item.id === model);
    setActiveIndex(Math.max(0, currentIndex));
  }, [allModels.length, model, open, provider, query]);

  useEffect(() => {
    setActiveIndex((current) => Math.min(current, Math.max(0, selectableModels.length - 1)));
  }, [query, selectableModels.length]);

  useEffect(() => {
    if (!open || !selectableModels.length) return;
    const frame = requestAnimationFrame(() => {
      const results = resultsRef.current;
      const row = results?.querySelector(`[data-model-index="${activeIndex}"]`);
      if (!results || !row) return;
      const resultsRect = results.getBoundingClientRect();
      const rowRect = row.getBoundingClientRect();
      const inset = 4;
      if (rowRect.top < resultsRect.top + inset) {
        results.scrollTop -= resultsRect.top + inset - rowRect.top;
      } else if (rowRect.bottom > resultsRect.bottom - inset) {
        results.scrollTop += rowRect.bottom - (resultsRect.bottom - inset);
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [activeIndex, open, selectableModels.length]);

  const applyDraft = (next) => onDraftChange?.({
    provider: next.provider,
    model: next.model,
    reasoningEffort: next.effort,
  });

  useEffect(() => {
    if (sessionID || !onDraftChange || provider || model || !directory) return;
    const fallback = providers.find((item) => item.ready && item.currentModel);
    if (!fallback) return;
    const nextEffort = fallback.currentReasoningEffort || "";
    setProvider(fallback.id);
    setModel(fallback.currentModel);
    setEffort(nextEffort);
    applyDraft({ provider: fallback.id, model: fallback.currentModel, effort: nextEffort });
  }, [directory, model, provider, providers, sessionID]);

  const commitModel = (choice) => {
    if (!choice?.providerReady) return;
    const providerInfo = providers.find((item) => item.id === choice.provider);
    const nextEffort = resolveModelEffort(choice, effort, providerInfo?.currentReasoningEffort || "");
    setProvider(choice.provider);
    setModel(choice.id);
    setEffort(nextEffort);
    setSelectionError("");
    if (sessionID) {
      const generation = selectionGenerationRef.current + 1;
      selectionGenerationRef.current = generation;
      void controller.setSelection(sessionID, choice.provider, choice.id, nextEffort)
        .then(() => {
          if (selectionGenerationRef.current !== generation) return;
          setOpen(false);
          setQuery("");
        })
        .catch((selectionFailure) => {
          if (selectionGenerationRef.current !== generation) return;
          setSelectionError(modelSelectionErrorMessage(selectionFailure));
          setOpen(true);
        });
    } else {
      applyDraft({ provider: choice.provider, model: choice.id, effort: nextEffort });
      setOpen(false);
      setQuery("");
    }
  };

  const commitEffort = (nextEffort) => {
    setEffort(nextEffort);
    setSelectionError("");
    if (sessionID && provider && model) {
      const generation = selectionGenerationRef.current + 1;
      selectionGenerationRef.current = generation;
      void controller.setSelection(sessionID, provider, model, nextEffort)
        .catch((selectionFailure) => {
          if (selectionGenerationRef.current !== generation) return;
          setSelectionError(modelSelectionErrorMessage(selectionFailure));
          setOpen(true);
        });
    } else {
      applyDraft({ provider, model, effort: nextEffort });
    }
  };

  const onSearchKeyDown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      requestAnimationFrame(() => triggerRef.current?.focus());
      return;
    }
    if (!selectableModels.length) return;
    if (["ArrowDown", "ArrowUp", "Home", "End", "PageDown", "PageUp"].includes(event.key)) {
      event.preventDefault();
      setActiveIndex((index) => modelNavigationIndex(index, selectableModels.length, event.key));
    } else if (event.key === "Enter") {
      event.preventDefault();
      commitModel(selectableModels[activeIndex]);
    }
  };

  const reasoningEfforts = selectedModel?.reasoningEfforts || [];
  const displayModel = selectedModel?.name || model || "Choose model";
  const displayProvider = provider || "Provider";
  let selectableIndex = -1;

  return html`<div class="selection-controls" aria-label="Model selection">
    <div class=${`model-picker ${open ? "is-open" : ""}`} ref=${rootRef}>
      <button
        ref=${triggerRef}
        type="button"
        class="model-picker__trigger"
        aria-haspopup="dialog"
        aria-expanded=${open}
        disabled=${disabled || !directory}
        onClick=${() => {
          setOpen((value) => !value);
          setQuery("");
          setError("");
          setSelectionError("");
        }}
        onKeyDown=${(event) => {
          if (open || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
          event.preventDefault();
          setOpen(true);
          setQuery("");
          setError("");
          setSelectionError("");
        }}
      >
        <span class="model-picker__trigger-mark" aria-hidden="true">M</span>
        <span class="model-picker__trigger-copy">
          <strong>${displayModel}</strong>
          <small>${displayProvider}</small>
        </span>
        <span class="model-picker__chevron" aria-hidden="true">${open ? "⌃" : "⌄"}</span>
      </button>
      ${open ? html`<div class="model-picker__panel" role="dialog" aria-label="Choose model">
        <div class="model-picker__search">
          <span aria-hidden="true">⌕</span>
          <input
            ref=${searchRef}
            type="search"
            value=${query}
            placeholder="Search provider or model"
            aria-label="Search models"
            onInput=${(event) => {
              setQuery(event.currentTarget.value);
              setActiveIndex(0);
            }}
            onKeyDown=${onSearchKeyDown}
          />
          <kbd>Esc</kbd>
        </div>
        <div class="model-picker__meta">
          <span>Models on this machine</span>
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate · <kbd>Enter</kbd> choose</span>
        </div>
        ${selectionError ? html`<div class="model-picker__selection-error" role="alert">
          <strong>Model unchanged</strong>
          <span>${selectionError}</span>
        </div>` : null}
        <div class="model-picker__results" role="listbox" ref=${resultsRef}>
          ${loading && !allModels.length ? html`<div class="model-picker__state"><span class="pending-spinner"></span> Loading models…</div>` : null}
          ${error ? html`<div class="model-picker__state model-picker__state--error">${error}</div>` : null}
          ${!loading && !error && !filteredModels.length ? html`<div class="model-picker__state">No matching models.</div>` : null}
          ${groups.map((group) => html`<section class="model-picker__group">
            <div class="model-picker__group-label">
              <span>${group.provider}</span>
              ${providers.find((item) => item.id === group.provider)?.ready === false ? html`<span>Unavailable</span>` : null}
            </div>
            ${group.models.map((item) => {
              if (item.providerReady) selectableIndex += 1;
              const rowIndex = selectableIndex;
              const isCurrent = item.provider === provider && item.id === model;
              const isActive = item.providerReady && rowIndex === activeIndex;
              return html`<button
                type="button"
                class=${`model-picker__row ${isActive ? "is-active" : ""} ${isCurrent ? "is-current" : ""}`}
                role="option"
                aria-selected=${isCurrent}
                data-model-index=${item.providerReady ? rowIndex : null}
                disabled=${!item.providerReady}
                onMouseEnter=${() => { if (item.providerReady) setActiveIndex(rowIndex); }}
                onClick=${() => commitModel(item)}
              >
                <span class="model-picker__row-mark" aria-hidden="true">${isCurrent ? "●" : ""}</span>
                <span class="model-picker__row-copy">
                  <strong>${item.name || item.id}</strong>
                  <small>${item.id}${item.name && item.name !== item.id ? ` · ${item.provider}` : ""}</small>
                </span>
                <span class="model-picker__row-facts">
                  ${formatContextWindow(item.contextWindowTokens) ? html`<span>${formatContextWindow(item.contextWindowTokens)}</span>` : null}
                  ${item.reasoningEfforts?.length ? html`<span>${item.reasoningEfforts.join(" · ")}</span>` : null}
                </span>
              </button>`;
            })}
          </section>`)}
        </div>
        <div class="model-picker__footer">
          <span>${provider && model ? `${provider}/${model}` : "Choose a model for this session"}</span>
          <span>${selectedModel?.capabilities?.images ? "images" : "text"}${selectedModel?.capabilities?.tools ? " · tools" : ""}</span>
        </div>
      </div>` : null}
    </div>
    ${reasoningEfforts.length ? html`<div class="effort-picker" aria-label="Reasoning effort">
      ${reasoningEfforts.map((value) => html`<button
            type="button"
            class=${value === effort ? "is-active" : ""}
            disabled=${disabled}
            aria-pressed=${value === effort}
            onClick=${() => commitEffort(value)}
          >${value}</button>`)}
    </div>` : null}
  </div>`;
}
