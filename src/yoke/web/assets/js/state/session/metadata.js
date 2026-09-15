import { api } from "../../api/client.js";
import { store } from "../store.js";
import { installSessionSummary, optimisticSessionPatch, restoreSessionSummary } from "../optimistic-projections.js";

export async function patchSession(host, sessionID, patch) {
  const before = store.getState();
  const previous = before.sessions[sessionID] || null;
  const activeIndex = before.sessionOrder.indexOf(sessionID);
  const archivedIndex = before.archivedOrder.indexOf(sessionID);
  const generation = (host.optimisticSessionGeneration.get(sessionID) || 0) + 1;
  const lifecycleEpoch = host.lifecycleEpoch;
  const ownsPreviousWorkspace = host.workspace.guard();
  host.optimisticSessionGeneration.set(sessionID, generation);
  const mutation = { generation, patch, previous, activeIndex, archivedIndex, lifecycleEpoch };
  host.sessionPendingMutations.set(
    sessionID,
    [...(host.sessionPendingMutations.get(sessionID) || []), mutation],
  );
  if (previous) {
    store.setState((state) => installSessionSummary(state, optimisticSessionPatch(previous, patch)));
  }

  const prior = host.sessionMutationChains.get(sessionID) || Promise.resolve();
  const task = prior.catch(() => {}).then(async () => {
    if (!host.ownsLifecycle(lifecycleEpoch)) return null;
    const ownsWorkspace = host.workspace.guard();
    const removePending = () => {
      const remaining = (host.sessionPendingMutations.get(sessionID) || [])
        .filter((item) => item.generation !== generation);
      if (remaining.length) host.sessionPendingMutations.set(sessionID, remaining);
      else host.sessionPendingMutations.delete(sessionID);
      return remaining;
    };
    const reconcile = (summary, owns, remaining) => {
      let visible = host.workspace.reconcileMetadata(sessionID, summary, owns);
      for (const item of remaining) visible = optimisticSessionPatch(visible, item.patch);
      const pendingSelection = host.pendingSelections.get(sessionID);
      return pendingSelection ? { ...visible, selection: pendingSelection } : visible;
    };
    try {
      const response = await api.patchSession(sessionID, patch);
      if (!host.ownsLifecycle(lifecycleEpoch)) return null;
      const visible = reconcile(response.data, ownsWorkspace, removePending());
      store.setState((state) => installSessionSummary(state, visible));
      return response.data;
    } catch (error) {
      if (!host.ownsLifecycle(lifecycleEpoch)) return null;
      const remaining = removePending();
      try {
        const ownsRefreshWorkspace = host.workspace.guard();
        const response = await api.getSession(sessionID);
        if (!host.ownsLifecycle(lifecycleEpoch)) return null;
        const visible = reconcile(response.data, ownsRefreshWorkspace, remaining);
        store.setState((state) => installSessionSummary(state, visible));
      } catch {
        if (!host.ownsLifecycle(lifecycleEpoch)) return null;
        if (previous) {
          const visible = reconcile(previous, ownsPreviousWorkspace, remaining);
          store.setState((state) => remaining.length
            ? installSessionSummary(state, visible)
            : restoreSessionSummary(state, visible, activeIndex, archivedIndex));
        }
      }
      throw error;
    }
  });
  const chained = task.finally(() => {
    if (host.sessionMutationChains.get(sessionID) === chained) {
      host.sessionMutationChains.delete(sessionID);
    }
  });
  host.sessionMutationChains.set(sessionID, chained);
  return chained;
}
