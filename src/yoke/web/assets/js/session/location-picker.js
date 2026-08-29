import { html, useEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { api } from "../api/client.js";
import {
  createLocationBrowseCoordinator,
  isLocationBrowseQuery,
  lastLocationPath,
  withTrailingSeparator,
} from "./location-picker-logic.js";

const MAX_RECENT_LOCATIONS = 7;

export function LocationPicker({ value = "", recentLocations = [], onChange }) {
  const [query, setQuery] = useState(value);
  const [open, setOpen] = useState(false);
  const [browse, setBrowse] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef(null);
  const coordinatorRef = useRef(null);
  if (coordinatorRef.current === null) coordinatorRef.current = createLocationBrowseCoordinator();
  const coordinator = coordinatorRef.current;

  useEffect(() => {
    setQuery(value || "");
  }, [value]);

  const browseMode = isLocationBrowseQuery(query);
  useEffect(() => {
    if (!open || !browseMode) {
      coordinator.invalidate();
      setBrowse(null);
      setLoading(false);
      setError("");
      setActiveIndex(-1);
      return undefined;
    }

    const requestedPath = query.trim();
    setLoading(true);
    setError("");
    const timer = setTimeout(() => {
      void coordinator.run(
        () => api.browseLocations(requestedPath),
        (response) => {
          setBrowse(response.data);
          setLoading(false);
          setError("");
          setActiveIndex(-1);
        },
        (requestError) => {
          setBrowse(null);
          setLoading(false);
          setError(requestError?.message || String(requestError));
          setActiveIndex(-1);
        },
      );
    }, 90);
    return () => {
      clearTimeout(timer);
      coordinator.invalidate();
    };
  }, [browseMode, coordinator, open, query]);

  const recentItems = useMemo(() => {
    if (browseMode) return [];
    const needle = query.trim().toLowerCase();
    return recentLocations
      .filter((item) => !needle || String(item.directory || "").toLowerCase().includes(needle))
      .slice(0, MAX_RECENT_LOCATIONS)
      .map((item) => ({ kind: "recent", name: lastLocationPath(item.directory), directory: item.directory }));
  }, [browseMode, query, recentLocations]);

  const items = useMemo(() => {
    if (!browseMode) return recentItems;
    if (!browse) return [];
    const next = [];
    if (browse.parentDirectory) {
      next.push({ kind: "up", name: "..", directory: browse.parentDirectory });
    }
    for (const entry of browse.entries || []) {
      next.push({ kind: "folder", name: entry.name, directory: entry.directory });
    }
    return next;
  }, [browse, browseMode, recentItems]);

  useEffect(() => {
    if (activeIndex < 0) return;
    const frame = requestAnimationFrame(() => {
      document.getElementById(`working-location-option-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [activeIndex]);

  const selectDirectory = (directory) => {
    onChange(directory);
    setQuery(directory);
    setOpen(false);
    setActiveIndex(-1);
  };
  const browseTo = (directory) => {
    const separator = browse?.separator || "/";
    setQuery(withTrailingSeparator(directory, separator));
    setOpen(true);
    setActiveIndex(-1);
  };
  const activateItem = (item) => {
    if (!item) return;
    if (item.kind === "recent") selectDirectory(item.directory);
    else browseTo(item.directory);
  };
  const useBrowsableDirectory = () => {
    if (!browse?.selectableDirectory) return;
    selectDirectory(browse.selectableDirectory);
  };
  const close = () => {
    setOpen(false);
    setQuery(value || "");
    setActiveIndex(-1);
  };
  const onKeyDown = (event) => {
    if (event.isComposing) return;
    if (event.key === "Escape" && open) {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && items.length) {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((current) => {
        if (event.key === "ArrowDown") return current < items.length - 1 ? current + 1 : 0;
        return current > 0 ? current - 1 : items.length - 1;
      });
      return;
    }
    if (event.key !== "Enter" || !open) return;
    if (activeIndex >= 0 && items[activeIndex]) {
      event.preventDefault();
      activateItem(items[activeIndex]);
      return;
    }
    if (browseMode && browse?.selectableDirectory) {
      event.preventDefault();
      useBrowsableDirectory();
    }
  };
  const onBlur = (event) => {
    if (rootRef.current?.contains(event.relatedTarget)) return;
    close();
  };

  const showPanel = open && (browseMode || recentItems.length > 0 || !query.trim());
  const selected = browse?.selectableDirectory || null;
  return html`<div class="location-picker" ref=${rootRef}>
    <label class="location-picker__label" for="draft-working-location">Working location</label>
    <div class=${`location-picker__field ${open ? "is-open" : ""}`}>
      <span class="location-picker__glyph" aria-hidden="true">›</span>
      <input
        id="draft-working-location"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded=${showPanel}
        aria-controls=${showPanel ? "working-location-options" : undefined}
        aria-activedescendant=${activeIndex >= 0 ? `working-location-option-${activeIndex}` : undefined}
        autocomplete="off"
        spellcheck="false"
        value=${query}
        placeholder="~/dev/project or /path/to/project"
        onFocus=${() => setOpen(true)}
        onBlur=${onBlur}
        onInput=${(event) => {
          setQuery(event.currentTarget.value);
          setOpen(true);
          setActiveIndex(-1);
        }}
        onKeyDown=${onKeyDown}
      />
      <span class="location-picker__field-status" aria-hidden="true">${loading ? html`<span class="pending-spinner"></span>` : "fs"}</span>
    </div>
    ${showPanel ? html`<div class="location-picker__panel">
      <div class="location-picker__header">
        <span>${browseMode ? "Folders on this machine" : "Recent locations"}</span>
        <span class="location-picker__keys"><kbd>↑</kbd><kbd>↓</kbd> navigate · <kbd>Enter</kbd> choose</span>
      </div>
      <div id="working-location-options" class="location-picker__items" role="listbox" aria-label="Working location options">
        ${loading ? html`<div class="location-picker__state">Reading folders…</div>` : null}
        ${!loading && error ? html`<div class="location-picker__state location-picker__state--error">${error}</div>` : null}
        ${!loading && !error && items.map((item, index) => html`
          <button
            id=${`working-location-option-${index}`}
            role="option"
            aria-selected=${activeIndex === index}
            class=${`location-picker__item ${activeIndex === index ? "is-active" : ""}`}
            onMouseDown=${(event) => event.preventDefault()}
            onMouseEnter=${() => setActiveIndex(index)}
            onClick=${() => activateItem(item)}
          >
            <span class="location-picker__item-icon" aria-hidden="true">${item.kind === "up" ? "↰" : "▰"}</span>
            <span class="location-picker__item-copy"><strong>${item.name}</strong><small>${item.directory}</small></span>
            <span class="location-picker__item-tag">${item.kind === "recent" ? "recent" : item.kind === "up" ? "up" : "folder"}</span>
          </button>
        `)}
        ${!loading && !error && !items.length ? html`<div class="location-picker__state">${browseMode ? "No matching folders." : "Type ~/ or / to browse the filesystem."}</div>` : null}
      </div>
      ${browseMode ? html`<div class="location-picker__footer">
        <span class="location-picker__footer-path">${selected || "Finish typing an existing folder."}</span>
        ${selected ? html`<button
          class="primary small"
          onMouseDown=${(event) => event.preventDefault()}
          onClick=${useBrowsableDirectory}
        >Use folder</button>` : null}
      </div>` : null}
    </div>` : null}
  </div>`;
}
