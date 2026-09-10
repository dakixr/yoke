import { api } from "../../api/client.js";
import { store } from "../../state/store.js";

export class ProcessInspectorState {
  constructor(owner) {
    this.owner = owner;
    this.outputRequests = new Map();
  }

  invalidate() { this.outputRequests.clear(); }

  async load(processID) {
    const owner = this.owner;
    const sessionID = store.getState().ui.selectedSessionID;
    const request = owner.nextRequest(sessionID || "", "process:selection");
    const selection = sessionID ? owner.selectedRequest(sessionID, "process") : null;
    if (sessionID && selection !== null) {
      store.setState((state) => {
        const data = state.sessionData[sessionID] || {};
        return {
          ...state,
          ui: { ...state.ui, inspector: { ...state.ui.inspector, processID, runtimeSessionID: null } },
          sessionData: {
            ...state.sessionData,
            [sessionID]: {
              ...data,
              processDetail: data.processDetail?.processID === processID ? data.processDetail : null,
              processDetailError: null,
              inspectorSelections: { ...data.inspectorSelections, process: { processID } },
            },
          },
        };
      });
    }
    try {
      const detail = await api.process(processID);
      if (!owner.ownsRequest(request)) return;
      const detailSessionID = detail.data.sessionID;
      if (!detailSessionID || (sessionID && detailSessionID !== sessionID)) return;
      if (selection !== null && !owner.ownsSelection(sessionID, "process", selection)) return;
      store.setState((state) => {
        const data = state.sessionData[detailSessionID] || {};
        return {
          ...state,
          sessionData: {
            ...state.sessionData,
            [detailSessionID]: {
              ...data,
              processDetail: detail.data,
              processDetailError: null,
              inspectorSelections: { ...data.inspectorSelections, process: { processID } },
            },
          },
        };
      });
    } catch (error) {
      if (!owner.ownsRequest(request) || !owner.ownsSelection(sessionID, "process", selection)) return;
      if (sessionID) owner.setSessionField(sessionID, "processDetailError", error?.message || String(error));
      throw error;
    }
  }

  find(processID) {
    for (const [sessionID, data] of Object.entries(store.getState().sessionData)) {
      if (data?.processDetail?.processID === processID) return { sessionID, detail: data.processDetail };
    }
    return null;
  }

  async refreshOutput(processID) {
    const owner = this.owner;
    const lifecycleEpoch = owner.lifecycleEpoch();
    const located = this.find(processID);
    if (!located) return;
    const selection = owner.selectedRequest(located.sessionID, "process");
    let request = this.outputRequests.get(processID);
    if (!request) {
      request = api.processOutput(processID, located.detail.output?.latestSeq || 0, 500);
      this.outputRequests.set(processID, request);
      void request.finally(() => {
        if (this.outputRequests.get(processID) === request) this.outputRequests.delete(processID);
      }).catch(() => {});
    }
    const response = await request;
    const { sessionID } = located;
    if (owner.lifecycleEpoch() !== lifecycleEpoch || !owner.ownsSelection(sessionID, "process", selection)) return;
    const current = store.getState().sessionData[sessionID]?.processDetail;
    if (current?.processID !== processID) return;
    const currentSeq = current.output?.latestSeq || 0;
    if (response.cursor.truncatedBefore > currentSeq) return this.refresh(processID);
    const chunks = response.data.filter((chunk) => chunk.seq > currentSeq);
    if (!chunks.length && response.cursor.next <= currentSeq) return;
    owner.setSessionField(sessionID, "processDetail", {
      ...current,
      output: {
        ...current.output,
        tail: `${current.output?.tail || ""}${chunks.map((chunk) => chunk.text).join("")}`,
        latestSeq: Math.max(currentSeq, response.cursor.next || 0),
      },
    });
  }

  async refresh(processID) {
    const owner = this.owner;
    const located = this.find(processID);
    if (!located) return;
    const request = owner.nextRequest(located.sessionID, `process:detail:${processID}`);
    const selection = owner.selectedRequest(located.sessionID, "process");
    const detail = await api.process(processID);
    if (!owner.ownsRequest(request) || !owner.ownsSelection(located.sessionID, "process", selection)) return;
    const sessionID = detail.data.sessionID;
    if (!sessionID) return;
    const current = store.getState().sessionData[sessionID]?.processDetail;
    if (current?.processID !== processID) return;
    owner.setSessionField(sessionID, "processDetail", (detail.data.output?.latestSeq || 0) < (current.output?.latestSeq || 0)
      ? { ...detail.data, output: current.output }
      : detail.data);
  }
}
