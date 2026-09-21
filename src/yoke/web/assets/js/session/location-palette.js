import { html, useEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { api } from "../api/client.js";
import { trapFocus } from "../lib/focus.js";
import { useModalFocus } from "../lib/modal-focus.js";
import {
  appendBrowsePathSegment,
  createLocationBrowseCoordinator,
  ensureBrowseDirectoryPath,
  filterLocationBrowseEntries,
  filterRecentLocations,
  getBrowseDirectoryPath,
  getBrowseParentPath,
  getLocationBrowsePath,
  hasTrailingPathSeparator,
  lastLocationPath,
} from "./location-picker-logic.js";

/**
 * Working location palette.
 *
 * The input is primary and nothing is selected until an arrow key or click
 * chooses a row. Enter and the footer button confirm that selection. Opening a
 * directory is a separate action so choosing a workspace never depends on an
 * overloaded Enter key.
 */
const HOME_BROWSE_QUERY = "~/";

export function LocationPalette({ value = "", recentLocations = [], onChange, onClose }) {
  // Browsing starts in the home directory, so reaching the filesystem never
  // costs a typed "~/" first. T3 Code seeds its add-project browse the same way.
  const [query, setQuery] = useState(HOME_BROWSE_QUERY);
  const [highlight, setHighlight] = useState(null);
  const [cache, setCache] = useState({});
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef(null);
  const inputRef = useRef(null);
  const coordinatorRef = useRef(null);
  if (coordinatorRef.current === null) coordinatorRef.current = createLocationBrowseCoordinator();
  const coordinator = coordinatorRef.current;
  useModalFocus(dialogRef, true);

  useEffect(() => {
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const browsePath = useMemo(() => getLocationBrowsePath(query), [query]);
  const browse = cache[browsePath.directoryPath] || null;

  const load = async (directoryPath) => {
    if (!directoryPath || cache[directoryPath]) return;
    setPending(true);
    await coordinator.run(
      () => api.browseLocations(directoryPath),
      (response) => {
        setCache((current) => ({ ...current, [directoryPath]: response.data }));
        setPending(false);
        setError("");
      },
      (requestError) => {
        setPending(false);
        setError(requestError?.message || String(requestError));
      },
    );
  };

  useEffect(() => {
    if (!browsePath.isBrowsing) {
      coordinator.invalidate();
      setPending(false);
      setError("");
      return undefined;
    }
    const timer = setTimeout(() => void load(browsePath.directoryPath), 90);
    return () => clearTimeout(timer);
  }, [browsePath.isBrowsing, browsePath.directoryPath]);

  const { visibleEntries, exactEntry } = useMemo(
    () => filterLocationBrowseEntries(browse?.entries, browsePath.filterQuery),
    [browse, browsePath.filterQuery],
  );
  const projects = useMemo(
    () => (browsePath.isBrowsing ? [] : filterRecentLocations(recentLocations, query)),
    [browsePath.isBrowsing, query, recentLocations],
  );

  // Derive the parent from the directory the server resolved, so "~/" does not
  // claim itself as its own parent before the listing lands.
  const resolvedDirectory = ensureBrowseDirectoryPath(
    browse?.browseDirectory || browsePath.directoryPath,
  );
  const parentCandidate = getBrowseParentPath(resolvedDirectory);
  const parentPath = browsePath.isBrowsing
    && parentCandidate !== null
    && parentCandidate !== resolvedDirectory
    ? parentCandidate
    : null;

  const items = useMemo(() => {
    if (!browsePath.isBrowsing) {
      return projects.map((project) => ({
        kind: "project",
        value: `project:${project.directory}`,
        name: lastLocationPath(project.directory),
        directory: project.directory,
      }));
    }
    const rows = [];
    if (parentPath) {
      rows.push({ kind: "up", value: "browse:up", name: "..", directory: parentPath });
    }
    for (const entry of visibleEntries) {
      rows.push({
        kind: "directory",
        value: `browse:${entry.directory}`,
        name: entry.name,
        directory: entry.directory,
      });
    }
    return rows;
  }, [browsePath.isBrowsing, parentPath, projects, visibleEntries]);

  useEffect(() => {
    if (highlight === null) return;
    if (!items.some((item) => item.value === highlight)) setHighlight(null);
  }, [highlight, items]);

  useEffect(() => {
    if (highlight === null) return undefined;
    const frame = requestAnimationFrame(() => {
      dialogRef.current
        ?.querySelector(`[data-palette-value="${cssEscape(highlight)}"]`)
        ?.scrollIntoView({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [highlight]);

  // Load the destination before the query moves, so the listing swaps in place
  // rather than emptying while the next directory is read.
  const navigate = async (nextQuery) => {
    await load(getBrowseDirectoryPath(nextQuery));
    setHighlight(null);
    setQuery(nextQuery);
  };
  const browseTo = (item) => navigate(
    item.kind === "up" ? item.directory : appendBrowsePathSegment(query, item.name),
  );
  const commit = (directory) => {
    if (!directory) return;
    onChange(directory);
    onClose();
  };
  const resolvedPath = hasTrailingPathSeparator(query)
    ? (browse?.browseDirectory || query.trim())
    : (exactEntry?.directory || query.trim());
  const highlightedItem = items.find((item) => item.value === highlight) || null;
  const canSubmitPath = browsePath.isBrowsing && query.trim().length > 0;
  const selectedItem = highlightedItem?.kind === "directory" || highlightedItem?.kind === "project"
    ? highlightedItem
    : null;
  const selectedPath = selectedItem?.directory || (canSubmitPath ? resolvedPath : null);
  const selectionLabel = selectedItem?.kind === "project" || !browsePath.isBrowsing
    ? "Select project"
    : "Select folder";
  const selectableItems = items.filter((item) => item.kind !== "up");

  const onKeyDown = (event) => {
    if (event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!selectableItems.length) return;
      const index = selectableItems.findIndex((item) => item.value === highlight);
      const next = event.key === "ArrowDown"
        ? (index < 0 || index >= selectableItems.length - 1 ? 0 : index + 1)
        : (index <= 0 ? selectableItems.length - 1 : index - 1);
      setHighlight(selectableItems[next].value);
      return;
    }
    if (event.key === "ArrowRight" && highlightedItem?.kind === "directory") {
      event.preventDefault();
      void browseTo(highlightedItem);
      return;
    }
    if (event.key === "ArrowLeft" && highlight !== null && parentPath) {
      event.preventDefault();
      void navigate(parentPath);
      return;
    }
    if (event.key === "Enter" && selectedPath) {
      event.preventDefault();
      commit(selectedPath);
    }
  };

  const groupLabel = browsePath.isBrowsing ? "Directories" : "Recent projects";
  return html`<div class="modal-backdrop" onMouseDown=${(event) => event.target === event.currentTarget && onClose()}>
    <div
      class="command-palette location-palette"
      role="dialog"
      aria-modal="true"
      aria-label="Choose a working location"
      tabindex="-1"
      ref=${dialogRef}
      onKeyDown=${(event) => trapFocus(dialogRef.current, event)}
    >
      <div class="command-search">
        <span aria-hidden="true">${browsePath.isBrowsing ? "▰" : "⌕"}</span>
        <input
          ref=${inputRef}
          value=${query}
          aria-label="Working location"
          placeholder="Type a path, or clear the field to search recent projects"
          autocomplete="off"
          spellcheck=${false}
          onInput=${(event) => {
            setHighlight(null);
            setQuery(event.currentTarget.value);
          }}
          onKeyDown=${onKeyDown}
        />
        ${pending ? html`<span class="pending-spinner" aria-hidden="true"></span>` : null}
      </div>
      <div class="command-results" role="group" aria-label=${groupLabel}>
        ${error ? html`<div class="command-empty command-empty--error" role="alert">${error}</div>` : null}
        ${items.length ? html`<div class="command-group">
          <div class="command-group__label">${groupLabel}</div>
          ${items.map((item) => html`
            <div
              key=${item.value}
              data-palette-value=${item.value}
              class=${`location-palette__row ${highlight === item.value ? "is-active" : ""}`}
            >
              <button
                type="button"
                class="location-palette__row-select"
                aria-pressed=${item.kind === "up" ? undefined : highlight === item.value}
                aria-label=${item.kind === "up" ? "Go to parent folder" : `Select ${item.kind === "project" ? "project" : "folder"} ${item.name}`}
                onMouseDown=${(event) => event.preventDefault()}
                onClick=${() => item.kind === "up" ? void browseTo(item) : setHighlight(item.value)}
              >
                <span>
                  <strong>${item.name}</strong>
                  <small>${item.directory}</small>
                </span>
                <span>${item.kind === "up" ? "up" : item.kind === "project" ? "recent" : "folder"}</span>
              </button>
              ${item.kind === "directory" ? html`<button
                type="button"
                class="location-palette__row-open"
                aria-label=${`Open folder ${item.name}`}
                onMouseDown=${(event) => event.preventDefault()}
                onClick=${() => void browseTo(item)}
              >Open <span aria-hidden="true">›</span></button>` : null}
            </div>
          `)}
        </div>` : error ? null : html`<div class="command-empty">${emptyMessage({ browsePath, pending, query, recentLocations })}</div>`}
      </div>
      <div class="command-footer location-palette__footer">
        <span class="location-palette__keys"><kbd>↑</kbd><kbd>↓</kbd> Choose <kbd>Enter</kbd> Select <kbd>←</kbd> Up <kbd>→</kbd> Open</span>
        <span class="command-footer__path" title=${selectedPath || value || ""}>${selectedPath || value || ""}</span>
        <div class="location-palette__actions">
          <button type="button" onClick=${onClose}>Cancel</button>
          <button
            type="button"
            class="primary small"
            disabled=${!selectedPath}
            onClick=${() => commit(selectedPath)}
          >${selectionLabel}</button>
        </div>
      </div>
    </div>
  </div>`;
}

function emptyMessage({ browsePath, pending, query, recentLocations }) {
  if (pending) return "Reading folders…";
  if (browsePath.isBrowsing) {
    return query.trim() ? "No folder here matches that name. Select folder to use the path as typed." : "No folders here.";
  }
  if (!recentLocations.length) return "No projects yet. Type ~/ or / to browse the filesystem.";
  return "No recent project matches. Type ~/ or / to browse the filesystem.";
}

function cssEscape(value) {
  return String(value).replace(/["\\]/g, "\\$&");
}
