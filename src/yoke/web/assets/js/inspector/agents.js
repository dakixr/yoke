import { html, useEffect, useLayoutEffect, useMemo, useState } from "../../vendor/htm-preact.js";
import { api } from "../api/client.js";
import { controller } from "../state/controller.js";
import { store } from "../state/store.js";
import { AgentRosterController } from "./agents/controller.js";
import { defaultAgentFilter } from "./agents/logic.js";
import { AgentsView } from "./agents/view.js";
import { useInspectorPreferences } from "./state/preferences.js";

export function AgentInspector({ sessionID, inspector, capabilities }) {
  const roster = useMemo(() => new AgentRosterController({ api, store }), []);
  const [state, setState] = useState(roster.getState());
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "agents", {
    filter: null, search: "", selectedID: null, scrollTop: 0, mobilePane: "list",
  });
  useLayoutEffect(() => {
    const unsubscribe = roster.subscribe(() => setState(roster.getState()));
    roster.start();
    return () => { unsubscribe(); roster.stop(); };
  }, [roster]);
  useEffect(() => {
    if (state.loaded && !preferences.filter) patchPreferences({ filter: defaultAgentFilter(state.rows, null) });
  }, [state.loaded]);
  useEffect(() => {
    if (inspector?.agentId) patchPreferences({ selectedID: inspector.agentId, mobilePane: "detail" });
  }, [inspector?.agentId]);
  const running = state.rows.some((run) => run.status === "running");
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const openProcess = capabilities?.features?.processInspector
    ? (run) => { void controller.openInspector("process", { runtimeSessionID: run.runtimeSessionID, from: { mode: "agents", agentId: run.agentId } }); }
    : null;
  const backToOrigin = inspector?.from ? () => { void controller.backInspector(); } : null;
  return html`<${AgentsView} state=${state} preferences=${preferences} patchPreferences=${patchPreferences}
    now=${running ? now : Date.now()} retry=${() => roster.refresh()} openProcess=${openProcess} backToOrigin=${backToOrigin} />`;
}
