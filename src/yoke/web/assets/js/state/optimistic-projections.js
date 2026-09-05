// @ts-check

import { mergeSessionInfo } from "./reducer.js";

export function optimisticSessionPatch(session, patch) {
  const now = new Date().toISOString();
  const has = (key) => Object.prototype.hasOwnProperty.call(patch, key);
  return {
    ...session,
    title: has("title") ? patch.title : session.title,
    pinned: has("pinned") ? Boolean(patch.pinned) : session.pinned,
    archivedAt: has("archived") ? patch.archived ? now : null : session.archivedAt,
    time: { ...(session.time || {}), updated: now },
  };
}

export function mergeServerSessionSummary(current, incoming, queueRevisions) {
  const knownRevision = queueRevisions.get(incoming.id);
  const merged = mergeSessionInfo(current, incoming, {
    preserveQueue: knownRevision !== undefined,
  });
  const incomingRevision = Number(incoming.queue?.revision);
  if (Number.isFinite(incomingRevision)) {
    queueRevisions.set(
      incoming.id,
      knownRevision === undefined ? incomingRevision : Math.max(knownRevision, incomingRevision),
    );
  }
  return merged;
}

export function installSessionLists(state, current, archived, queueRevisions) {
  const sessions = { ...state.sessions };
  for (const session of [...current.data, ...archived.data]) {
    sessions[session.id] = mergeServerSessionSummary(
      sessions[session.id],
      session,
      queueRevisions,
    );
  }
  return {
    ...state,
    sessions,
    sessionOrder: current.data.map((session) => session.id),
    archivedOrder: archived.data.map((session) => session.id),
    archivedTotal: Number.isFinite(archived.total) ? archived.total : archived.data.length,
    sessionsCursor: current.cursor?.next || null,
    archivedCursor: archived.cursor?.next || null,
  };
}

export function installSessionSummary(state, session, { moveToFront = false } = {}) {
  const sessionID = session.id;
  const previous = state.sessions[sessionID] || null;
  const merged = mergeSessionInfo(previous, session, { preserveQueue: true });
  const activeIndex = state.sessionOrder.indexOf(sessionID);
  const archivedIndex = state.archivedOrder.indexOf(sessionID);
  const sessionOrder = state.sessionOrder.filter((id) => id !== sessionID);
  const archivedOrder = state.archivedOrder.filter((id) => id !== sessionID);
  const target = merged.archivedAt ? archivedOrder : sessionOrder;
  const previousIndex = merged.archivedAt ? archivedIndex : activeIndex;
  const changedShelf = previous
    ? Boolean(previous.archivedAt) !== Boolean(merged.archivedAt)
    : previousIndex < 0;
  if (moveToFront || changedShelf || previousIndex < 0) target.unshift(sessionID);
  else target.splice(Math.min(previousIndex, target.length), 0, sessionID);
  return {
    ...state,
    sessions: { ...state.sessions, [sessionID]: merged },
    sessionOrder,
    archivedOrder,
  };
}

export function restoreSessionSummary(state, session, activeIndex, archivedIndex) {
  const sessionID = session.id;
  const merged = mergeSessionInfo(state.sessions[sessionID], session, { preserveQueue: true });
  const sessionOrder = state.sessionOrder.filter((id) => id !== sessionID);
  const archivedOrder = state.archivedOrder.filter((id) => id !== sessionID);
  if (activeIndex >= 0) sessionOrder.splice(Math.min(activeIndex, sessionOrder.length), 0, sessionID);
  if (archivedIndex >= 0) archivedOrder.splice(Math.min(archivedIndex, archivedOrder.length), 0, sessionID);
  return {
    ...state,
    sessions: { ...state.sessions, [sessionID]: merged },
    sessionOrder,
    archivedOrder,
  };
}

export function applyQueueOperations(queue, operations) {
  const items = [...(queue?.items || [])];
  for (const operation of operations || []) {
    const index = items.findIndex((item) => item.id === operation.id);
    if (operation.op === "remove") {
      if (index >= 0) items.splice(index, 1);
      continue;
    }
    if (index < 0) continue;
    if (operation.op === "update") {
      items[index] = { ...items[index], prompt: operation.prompt };
      continue;
    }
    if (operation.op === "setDelivery") {
      items[index] = { ...items[index], delivery: operation.delivery };
      continue;
    }
    if (operation.op === "setPaused") {
      items[index] = { ...items[index], paused: operation.paused };
      continue;
    }
    const [item] = items.splice(index, 1);
    if (operation.op === "moveToStart") items.unshift(item);
    else if (operation.op === "moveBefore") {
      const targetID = operation.beforeID ?? operation.before_id;
      const target = items.findIndex((candidate) => candidate.id === targetID);
      items.splice(target >= 0 ? target : items.length, 0, item);
    } else if (operation.op === "moveAfter") {
      const targetID = operation.afterID ?? operation.after_id;
      const target = items.findIndex((candidate) => candidate.id === targetID);
      items.splice(target >= 0 ? target + 1 : items.length, 0, item);
    } else items.splice(Math.min(index, items.length), 0, item);
  }
  return { ...queue, items };
}

export function queueSummary(queue) {
  const items = queue?.items || [];
  return {
    total: items.length,
    steering: items.filter((item) => item.delivery === "steer" && !item.paused).length,
    queued: items.filter((item) => item.delivery === "queue" && !item.paused).length,
    paused: items.filter((item) => item.paused).length,
    revision: queue?.revision || 0,
  };
}
