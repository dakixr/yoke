import { store } from "../../state/store.js";

/** HEAD can precede the newest page. Fetch actual history, never jump to a guess. */
export async function revealTreeHead(owner, sessionID) {
  const selection = owner.selectedRequest(sessionID, "tree");
  if (selection === null) return null;
  const request = owner.nextRequest(sessionID, "tree:head");
  const owns = () => owner.ownsRequest(request) && owner.ownsSelection(sessionID, "tree", selection);
  let tree = store.getState().sessionData[sessionID]?.tree;
  if (!tree) {
    await owner.refreshTree(sessionID);
    if (!owns()) return null;
    tree = store.getState().sessionData[sessionID]?.tree;
  }
  const seenCursors = new Set();
  while (tree?.leafID && !tree.entries?.some((entry) => entry.id === tree.leafID)) {
    const cursor = tree.cursor?.next;
    if (!cursor || seenCursors.has(cursor)) {
      owner.notice("The current position is outside the available history. Refresh Tree to try again.");
      return null;
    }
    seenCursors.add(cursor);
    await owner.loadMoreTree(sessionID);
    if (!owns()) return null;
    tree = store.getState().sessionData[sessionID]?.tree;
  }
  return owns() ? tree : null;
}
