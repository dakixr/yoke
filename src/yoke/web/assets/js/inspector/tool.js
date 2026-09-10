import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";
import { useInspectorPreferences } from "./state/preferences.js";
import { ActivityDetail } from "./activity/detail.js";
import { ActivityList } from "./activity/list.js";
import { filterCalls, newCallCount } from "./activity/logic.js";

const DEFAULTS = {
  search: "", wrap: true, callID: null,
  pane: "list", following: true, scrollTop: 0, anchorID: null, anchorOffset: 0,
  lastObservedID: null, lastObservedSequence: null, newCalls: 0, detailPositions: {},
};

export function ToolInspector(props) {
  return html`<${ActivityInspector} key=${props.sessionID} ...${props} />`;
}

function ActivityInspector({ sessionID, inspector, data }) {
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "tool", DEFAULTS);
  const [busy, setBusy] = useState("");
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState(null);
  const restoredSelection = useRef(false);
  const previousSelection = useRef(preferences.callID);
  const backRef = useRef(null);
  // Shared state already supplies canonical session order. Do not reinterpret
  // start timestamps here: a pending call can acquire its timestamp later.
  const calls = useMemo(() => data?.toolCalls || [], [data?.toolCalls]);
  const visible = useMemo(() => filterCalls(calls, preferences.search), [calls, preferences.search]);
  const selectedID = inspector.callID || preferences.callID;
  const currentListError = listError || (!data?.toolCalls ? data?.inspectorErrors?.tool : "") || "";
  const currentDetailError = detailError?.id === selectedID ? detailError.message : data?.toolDetailError;
  // Never render stale detail under a newly selected row.
  const detail = selectedID && data?.toolDetail?.id === selectedID ? data.toolDetail : null;

  useLayoutEffect(() => {
    if (preferences.pane === "detail" && backRef.current?.offsetParent) backRef.current.focus({ preventScroll: true });
  }, [preferences.pane, selectedID]);

  useEffect(() => {
    if (restoredSelection.current) return;
    restoredSelection.current = true;
    if (!inspector.callID && preferences.callID) {
      void controller.selectToolCall(sessionID, preferences.callID).catch((error) => setDetailError({ id: preferences.callID, message: error?.message || String(error) }));
    }
  }, [sessionID]);

  useEffect(() => {
    if (!inspector.callID) return;
    const changed = previousSelection.current !== inspector.callID;
    previousSelection.current = inspector.callID;
    patchPreferences({ callID: inspector.callID, ...(changed ? { pane: "detail" } : {}) });
  }, [inspector.callID]);

  useEffect(() => {
    const newestID = calls.at(-1)?.id;
    if (!newestID || newestID === preferences.lastObservedID) return;
    patchPreferences((previous) => ({
      lastObservedID: newestID,
      lastObservedSequence: calls.at(-1)?.sequence ?? null,
      newCalls: previous.following ? 0 : previous.newCalls + newCallCount(calls, previous.lastObservedID, previous.lastObservedSequence),
    }));
  }, [calls]);

  useEffect(() => {
    if (!inspector.callID || detail) return;
    let active = true;
    setDetailError(null);
    void controller.loadToolCall(sessionID, inspector.callID).catch((error) => {
      if (active) setDetailError({ id: inspector.callID, message: error?.message || String(error) });
    });
    return () => { active = false; };
  }, [sessionID, inspector.callID, detail?.id]);

  useEffect(() => {
    if (!detail || !["running", "pending"].includes(detail.status)) return;
    let active = true;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        await controller.loadToolCall(sessionID, detail.id);
        if (active) setDetailError(null);
      } catch (error) {
        if (active) setDetailError({ id: detail.id, message: `Live update failed; retrying. ${error?.message || String(error)}` });
      } finally {
        pending = false;
      }
    }, 600);
    return () => { active = false; window.clearInterval(timer); };
  }, [sessionID, detail?.id, detail?.status]);

  const request = async (kind, action) => {
    if (busy) return;
    setBusy(kind);
    setListError("");
    try { await action(); }
    catch (error) { setListError(error?.message || String(error)); }
    finally { setBusy(""); }
  };
  const refresh = () => request("refresh", () => controller.listToolCalls(sessionID));
  const select = (callID) => {
    patchPreferences({ callID, pane: "detail", ...(callID !== calls.at(-1)?.id ? { following: false } : {}) });
    setDetailError(null);
    void controller.selectToolCall(sessionID, callID).catch((error) => setDetailError({ id: callID, message: error?.message || String(error) }));
  };
  const retryDetail = () => {
    setDetailError(null);
    void controller.loadToolCall(sessionID, selectedID).catch((error) => setDetailError({ id: selectedID, message: error?.message || String(error) }));
  };

  if (!data?.toolCalls && !selectedID) return html`<div class="activity-empty" aria-live="polite">
    <strong>${currentListError ? "Could not load tool activity" : "Loading tool activity…"}</strong>
    <p>${currentListError || "Calls will appear in start order."}</p>
    <button disabled=${Boolean(busy)} onClick=${refresh}>${busy ? "Loading…" : "Retry loading"}</button>
  </div>`;

  return html`<div class=${`tool-activity ${preferences.pane === "detail" && selectedID ? "has-mobile-detail" : ""}`}>
    <${ActivityList} calls=${calls} visible=${visible} total=${data?.toolCallsTotal} cursor=${data?.toolCallsCursor}
      selectedID=${selectedID} detail=${detail} preferences=${preferences} patchPreferences=${patchPreferences}
      busy=${busy} error=${currentListError} onRefresh=${refresh} onSelect=${select}
      loading=${!data?.toolCalls && !currentListError} windowChanged=${data?.toolCallsWindowChanged}
      onLoadEarlier=${() => {
        patchPreferences({ following: false });
        return request("earlier", () => controller.loadMoreToolCalls(sessionID));
      }}
      onLatest=${() => request("latest", async () => {
        await controller.showLatestToolCalls(sessionID);
        patchPreferences({ following: true, newCalls: 0, search: "", anchorID: null });
      })}
    />
    <section class="activity-detail-pane" aria-label="Selected tool call">
      <div class="activity-mobile-back"><button ref=${backRef} onClick=${() => patchPreferences({ pane: "list" })}>Back to tool activity</button></div>
      ${currentDetailError ? html`<div class="activity-request-error" role="alert"><span>${currentDetailError}</span><button onClick=${retryDetail}>Retry detail</button></div>` : null}
      ${detail ? html`<${ActivityDetail} key=${`${sessionID}:${detail.id}`} detail=${detail} preferences=${preferences} patchPreferences=${patchPreferences} />`
        : html`<div class="activity-empty" aria-live="polite"><strong>${selectedID ? currentDetailError ? "Tool call unavailable" : "Loading selected call…" : "Select a tool call"}</strong>
          <p>${selectedID ? "The selected row stays highlighted while its detail loads." : "Inspect the exact call arguments and returned result. New calls never change your selection."}</p>
        </div>`}
    </section>
  </div>`;
}
