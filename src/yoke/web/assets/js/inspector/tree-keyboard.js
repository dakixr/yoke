const NAVIGATION_KEYS = new Set([
  "ArrowDown",
  "ArrowUp",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "PageDown",
  "PageUp",
]);

export function isTreeNavigationKey(key) {
  return NAVIGATION_KEYS.has(key);
}

export function treeKeyboardTarget(entries, currentID, key, { pageSize = 5 } = {}) {
  const rows = entries || [];
  if (!rows.length) return null;
  const foundIndex = rows.findIndex((entry) => entry.id === currentID);
  const currentIndex = foundIndex >= 0 ? foundIndex : 0;
  const current = rows[currentIndex];

  if (key === "Home") return rows[0].id;
  if (key === "End") return rows.at(-1).id;
  if (key === "ArrowUp") return rows[Math.max(0, currentIndex - 1)].id;
  if (key === "ArrowDown") return rows[Math.min(rows.length - 1, currentIndex + 1)].id;
  if (key === "PageUp") return rows[Math.max(0, currentIndex - pageSize)].id;
  if (key === "PageDown") return rows[Math.min(rows.length - 1, currentIndex + pageSize)].id;
  if (key === "ArrowLeft") return current.graphParentID || current.id;
  if (key === "ArrowRight") {
    const children = rows.filter((entry) => entry.graphParentID === current.id);
    if (!children.length) return current.id;
    return (children.find((entry) => entry.active || entry.current) || children[0]).id;
  }
  return current.id;
}
