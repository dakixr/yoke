export const TREE_GRAPH_ROW_HEIGHT = 58;

export function displayTreeEntries(allEntries, visibleEntries) {
  const allByID = new Map((allEntries || []).map((entry) => [entry.id, entry]));
  const visibleIDs = new Set((visibleEntries || []).map((entry) => entry.id));
  const resolvedParents = new Map();
  return (visibleEntries || []).map((entry) => {
    const originalParentID = entry.parentID || null;
    const parentID = nearestVisibleParent(originalParentID, allByID, visibleIDs, resolvedParents);
    return {
      ...entry,
      graphParentID: parentID && visibleIDs.has(parentID) ? parentID : null,
      graphExternalParent: Boolean(originalParentID && (!parentID || !visibleIDs.has(parentID))),
    };
  });
}

export function treeGraphLayout(entries) {
  const rows = entries || [];
  const byID = new Map(rows.map((entry) => [entry.id, entry]));
  const indexByID = new Map(rows.map((entry, index) => [entry.id, index]));
  const children = new Map(rows.map((entry) => [entry.id, []]));
  for (const entry of rows) {
    if (entry.graphParentID && children.has(entry.graphParentID)) {
      children.get(entry.graphParentID).push(entry.id);
    }
  }

  const laneByID = new Map();
  for (const entry of rows) {
    if (entry.active || entry.current) laneByID.set(entry.id, 0);
  }

  const laneIntervals = new Map();
  const leaves = rows
    .filter((entry) => !(children.get(entry.id)?.length))
    .sort((left, right) => {
      if (left.current !== right.current) return left.current ? -1 : 1;
      return (indexByID.get(right.id) || 0) - (indexByID.get(left.id) || 0);
    });

  for (const leaf of leaves) assignBranchLane(leaf.id, byID, indexByID, laneByID, laneIntervals);
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const entry = rows[index];
    if (!laneByID.has(entry.id)) {
      assignBranchLane(entry.id, byID, indexByID, laneByID, laneIntervals);
    }
  }

  const laneCount = Math.max(0, ...laneByID.values()) + 1;
  const laneGap = Math.max(13, Math.min(24, 230 / Math.max(1, laneCount)));
  const graphWidth = Math.ceil(42 + Math.max(0, laneCount - 1) * laneGap);
  const xForLane = (lane) => 21 + lane * laneGap;
  const yForRow = (row) => row * TREE_GRAPH_ROW_HEIGHT + TREE_GRAPH_ROW_HEIGHT / 2;

  const nodes = rows.map((entry, row) => ({
    id: entry.id,
    row,
    lane: laneByID.get(entry.id) || 0,
    x: xForLane(laneByID.get(entry.id) || 0),
    y: yForRow(row),
    current: Boolean(entry.current),
    active: Boolean(entry.active),
    externalParent: Boolean(entry.graphExternalParent),
  }));
  const nodeByID = new Map(nodes.map((node) => [node.id, node]));
  const edges = [];
  for (const entry of rows) {
    if (!entry.graphParentID) continue;
    const parent = nodeByID.get(entry.graphParentID);
    const child = nodeByID.get(entry.id);
    if (!parent || !child) continue;
    edges.push({
      parentID: parent.id,
      childID: child.id,
      lane: child.lane,
      path: edgePath(parent, child),
    });
  }

  return {
    rows,
    nodes,
    edges,
    laneCount,
    graphWidth,
    height: rows.length * TREE_GRAPH_ROW_HEIGHT,
  };
}

function assignBranchLane(startID, byID, indexByID, laneByID, laneIntervals) {
  if (laneByID.has(startID)) return;
  const path = [];
  let currentID = startID;
  while (currentID && !laneByID.has(currentID)) {
    const entry = byID.get(currentID);
    if (!entry) break;
    path.push(currentID);
    currentID = entry.graphParentID || null;
  }
  if (!path.length) return;
  const indexes = path.map((id) => indexByID.get(id)).filter((value) => value != null);
  if (currentID && indexByID.has(currentID)) indexes.push(indexByID.get(currentID));
  const interval = [Math.min(...indexes), Math.max(...indexes)];
  const lane = availableLane(interval, laneIntervals);
  for (const id of path) laneByID.set(id, lane);
  const intervals = laneIntervals.get(lane) || [];
  intervals.push(interval);
  laneIntervals.set(lane, intervals);
}

function availableLane(interval, laneIntervals) {
  for (let lane = 1; lane < 64; lane += 1) {
    const intervals = laneIntervals.get(lane) || [];
    const overlaps = intervals.some(([start, end]) => !(interval[1] < start || interval[0] > end));
    if (!overlaps) return lane;
  }
  return 63;
}

function edgePath(parent, child) {
  if (parent.lane === child.lane) return `M ${parent.x} ${parent.y} L ${child.x} ${child.y}`;
  const bend = Math.min(24, Math.max(12, (child.y - parent.y) / 3));
  return `M ${parent.x} ${parent.y} C ${parent.x} ${parent.y + bend}, ${child.x} ${child.y - bend}, ${child.x} ${child.y}`;
}

function nearestVisibleParent(startID, allByID, visibleIDs, cache) {
  if (!startID) return null;
  if (visibleIDs.has(startID)) return startID;
  if (cache.has(startID)) return cache.get(startID);
  const trail = [];
  const visited = new Set();
  let currentID = startID;
  while (currentID && !visibleIDs.has(currentID) && !visited.has(currentID)) {
    if (cache.has(currentID)) {
      currentID = cache.get(currentID);
      break;
    }
    visited.add(currentID);
    trail.push(currentID);
    currentID = allByID.get(currentID)?.parentID || null;
  }
  const resolved = currentID && visibleIDs.has(currentID) ? currentID : null;
  for (const id of trail) cache.set(id, resolved);
  return resolved;
}
