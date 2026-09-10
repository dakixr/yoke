import { html } from "../../vendor/htm-preact.js";
import { useInspectorPreferences } from "./state/preferences.js";
import { ProcessView } from "./process/view.js";

export function ProcessInspector({ sessionID, data, capabilities, inspector }) {
  const [preferences, patchPreferences] = useInspectorPreferences(sessionID, "process", {
    filter: null, search: "", selectedID: null, scrollTop: 0, mainScrollTop: 0, mobilePane: "list", wrap: true, outputs: {},
  });
  return html`<${ProcessView} key=${sessionID} sessionID=${sessionID} data=${data} capabilities=${capabilities} inspector=${inspector} preferences=${preferences} patchPreferences=${patchPreferences} />`;
}
