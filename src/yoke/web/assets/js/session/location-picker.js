import { html, useState } from "../../vendor/htm-preact.js";
import { LocationPalette } from "./location-palette.js";
import { lastLocationPath } from "./location-picker-logic.js";

/**
 * Working location control. The trigger shows the committed location and opens
 * the palette, which owns browsing and selection.
 */
export function LocationPicker({ value = "", recentLocations = [], onChange, label = "Working location" }) {
  const [open, setOpen] = useState(false);
  const name = value ? lastLocationPath(value) : "Choose a working location";
  return html`<div class="location-picker">
    <span class="location-picker__label" id="working-location-label">${label}</span>
    <button
      id="draft-working-location"
      class=${`location-picker__trigger ${value ? "" : "is-empty"}`}
      type="button"
      aria-haspopup="dialog"
      aria-expanded=${open}
      aria-labelledby="working-location-label"
      onClick=${() => setOpen(true)}
    >
      <span class="location-picker__trigger-glyph" aria-hidden="true">▰</span>
      <span class="location-picker__trigger-copy">
        <strong>${name}</strong>
        ${value ? html`<small>${value}</small>` : null}
      </span>
      <span class="location-picker__trigger-hint" aria-hidden="true">Change</span>
    </button>
    ${open ? html`<${LocationPalette}
      value=${value}
      recentLocations=${recentLocations}
      onChange=${onChange}
      onClose=${() => setOpen(false)}
    />` : null}
  </div>`;
}
