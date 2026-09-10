import { useCallback, useRef } from "../../../vendor/htm-preact.js";
import { useStore } from "../../state/hooks.js";
import { inspectorPreferences, patchInspectorPreferences } from "./view-memory.js";

/** A shallow-merge setter, scoped to this session and inspector view. */
export function useInspectorPreferences(sessionID, mode, defaults = {}) {
  const defaultsRef = useRef(defaults);
  defaultsRef.current = defaults;
  const saved = useStore((state) => inspectorPreferences(state, sessionID, mode));
  const patch = useCallback((update) => {
    patchInspectorPreferences(sessionID, mode, update, defaultsRef.current);
  }, [sessionID, mode]);
  return [{ ...defaults, ...saved }, patch];
}
