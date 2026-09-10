import { html, useEffect, useLayoutEffect, useRef, useState } from "../../../vendor/htm-preact.js";

// Feedback is view-local. The controller still owns optimistic state and rollback.
export function useActions() {
  const [feedback, setFeedback] = useState({});
  const pending = useRef(new Set());
  const generations = useRef(new Map());
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const run = async (key, task, success = "Saved", { replace = false } = {}) => {
    if (pending.current.has(key) && !replace) return false;
    const generation = (generations.current.get(key) || 0) + 1;
    generations.current.set(key, generation);
    const owns = () => mounted.current && generations.current.get(key) === generation;
    pending.current.add(key);
    setFeedback((state) => ({ ...state, [key]: { pending: true } }));
    try {
      await task();
      if (owns()) setFeedback((state) => ({ ...state, [key]: { success } }));
      return owns();
    } catch (error) {
      if (owns()) setFeedback((state) => ({ ...state, [key]: { error: error?.message || String(error) } }));
      return false;
    } finally {
      if (generations.current.get(key) === generation) pending.current.delete(key);
    }
  };
  const clear = () => setFeedback({});
  return { feedback, run, clear };
}

export function Feedback({ state, pendingLabel = "Saving…" }) {
  if (!state) return null;
  return html`<span class=${`support-feedback ${state.error ? "support-feedback--error" : ""}`} role=${state.error ? "alert" : "status"}>${state.pending ? pendingLabel : state.error || state.success}</span>`;
}

export function ScrollArea({ preferences, patchPreferences, children, className = "" }) {
  const ref = useRef(null);
  useLayoutEffect(() => { if (ref.current) ref.current.scrollTop = preferences.scrollTop || 0; }, []);
  return html`<div ref=${ref} class=${`support-scroll ${className}`} onScroll=${(event) => patchPreferences({ scrollTop: event.currentTarget.scrollTop })}>${children}</div>`;
}

export function LoadState({ error, label }) {
  return html`<div class=${error ? "support-feedback support-feedback--error" : "inspector-loading"} role=${error ? "alert" : "status"}>${error?.message || error || label}</div>`;
}
