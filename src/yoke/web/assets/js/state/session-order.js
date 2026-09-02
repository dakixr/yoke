// @ts-check

export function visualSessionOrder(sessionOrder, sessions) {
  const pinned = [];
  const inbox = [];
  for (const id of sessionOrder || []) {
    const session = sessions?.[id];
    if (!session) continue;
    if (session.pinned) pinned.push(id);
    else inbox.push(id);
  }
  return [...pinned, ...inbox];
}

export function adjacentVisualSessionID(sessionOrder, sessions, selectedSessionID, delta) {
  const ids = visualSessionOrder(sessionOrder, sessions);
  if (!ids.length) return null;
  const current = ids.indexOf(selectedSessionID);
  if (current < 0) return delta < 0 ? ids.at(-1) : ids[0];
  return ids[(current + delta + ids.length) % ids.length];
}
