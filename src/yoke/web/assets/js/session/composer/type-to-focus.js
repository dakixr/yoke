import { useLayoutEffect, useRef } from "../../../vendor/htm-preact.js";

// Type-to-focus, ported from T3 Code's chat view. Text that lands outside any
// editable surface is redirected into the composer, so writing never costs a
// click first.
const EDITABLE_SELECTOR = [
  "input",
  "textarea",
  "select",
  '[contenteditable="true"]',
  '[contenteditable="plaintext-only"]',
  '[role="textbox"]',
].join(",");

// Anything that owns the keyboard while it is on screen. Yoke's palettes and
// menus live behind these, and the composer is not reachable underneath them.
const FLOATING_LAYER_SELECTOR = [
  '[role="dialog"][aria-modal="true"]',
  ".modal-backdrop",
  "details[open]",
].join(",");

function eventPathContainsSelector(event, selector) {
  const path = event.composedPath?.() || [];
  const targets = path.length ? path : [event.target];
  return targets.some((target) => target instanceof Element && target.closest(selector));
}

/**
 * Unlike T3 Code this does not skip buttons and links. Closing a yoke dialog
 * restores focus to the control that opened it, so a resting button is the
 * common case rather than a deliberate target. Enter and Space never redirect,
 * so those controls keep the keys that activate them.
 */
export function shouldRedirectInputToComposer(event) {
  if (event.defaultPrevented) return false;
  if (eventPathContainsSelector(event, EDITABLE_SELECTOR)) return false;
  return !document.querySelector(FLOATING_LAYER_SELECTOR);
}

/** The character a keystroke should add to the composer, or null to ignore it. */
export function typedComposerText(event) {
  if (event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return null;
  if (event.key.length !== 1) return null;
  // Space belongs to the transcript, which it pages through.
  if (event.key === " ") return null;
  return shouldRedirectInputToComposer(event) ? event.key : null;
}

/** Plain text pasted with nothing editable focused. Files stay with the composer. */
export function pastedComposerText(event) {
  const clipboard = event.clipboardData;
  if (!clipboard || clipboard.files?.length) return null;
  if (!shouldRedirectInputToComposer(event)) return null;
  return clipboard.getData("text/plain") || null;
}

export function useTypeToFocus({ enabled, inputRef, appendText }) {
  const appendRef = useRef(appendText);
  appendRef.current = appendText;

  // A layout effect, so the composer accepts text from the commit that mounts
  // it and stops accepting it in the commit that disables it.
  useLayoutEffect(() => {
    if (!enabled) return undefined;
    const redirect = (event, text) => {
      event.preventDefault();
      event.stopPropagation();
      appendRef.current(text);
      requestAnimationFrame(() => {
        const input = inputRef.current;
        if (!input) return;
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
    };
    const onKeyDown = (event) => {
      const text = typedComposerText(event);
      if (text !== null) redirect(event, text);
    };
    const onPaste = (event) => {
      const text = pastedComposerText(event);
      if (text !== null) redirect(event, text);
    };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("paste", onPaste, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("paste", onPaste, true);
    };
  }, [enabled, inputRef]);
}
