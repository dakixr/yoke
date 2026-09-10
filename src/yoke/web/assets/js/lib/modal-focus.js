import { useLayoutEffect } from "../../vendor/htm-preact.js";

const modalStack = [];
const inertOwners = new WeakMap();

/** Nested dialogs share inert ownership so closing one cannot unlock another. */
export function useModalFocus(ref, enabled = true) {
  useLayoutEffect(() => {
    const dialog = ref.current;
    if (!enabled || !dialog) return undefined;
    const opener = document.activeElement;
    const releases = [];
    modalStack.push(dialog);
    for (let branch = dialog; branch?.parentElement && branch !== document.body; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling === branch || ["SCRIPT", "STYLE", "LINK"].includes(sibling.tagName)) continue;
        if (sibling.classList.contains("notice-toast")) continue;
        releases.push(holdInert(sibling));
      }
    }
    // A child such as Tree can choose its own initial row in the next frame.
    dialog.focus({ preventScroll: true });
    return () => {
      const index = modalStack.indexOf(dialog);
      if (index >= 0) modalStack.splice(index, 1);
      for (const release of releases) release();
      const top = modalStack.at(-1);
      const visibleOpener = opener?.isConnected && opener.getClientRects?.().length && !opener.closest?.("[inert]");
      const target = visibleOpener ? opener : document.querySelector("[data-inspector-opener]");
      if (target && (!top || top.contains(target))) target.focus({ preventScroll: true });
    };
  }, [enabled]);
}

function holdInert(node) {
  let record = inertOwners.get(node);
  if (!record) {
    record = { count: 0, original: node.inert };
    inertOwners.set(node, record);
  }
  record.count += 1;
  node.inert = true;
  return () => {
    record.count -= 1;
    if (record.count) return;
    node.inert = record.original;
    inertOwners.delete(node);
  };
}
