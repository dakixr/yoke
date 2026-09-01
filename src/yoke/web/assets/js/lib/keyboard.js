// @ts-check

export function installKeybindings(actions) {
  let lastEscapeAt = 0;
  const handler = (event) => {
    const command = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (event.key !== "Escape") lastEscapeAt = 0;
    if (command && event.shiftKey && !event.altKey && key === "o") return invoke(event, actions.newSession);
    if (command && key === "k") return invoke(event, actions.palette);
    if (command && !event.shiftKey && !event.altKey && key === "b") {
      return invoke(event, actions.toggleSidebar);
    }
    if (event.key === "Escape") {
      const now = performance.now();
      if (now - lastEscapeAt <= 650 && actions.interrupt?.()) {
        lastEscapeAt = 0;
        event.preventDefault();
        return;
      }
      const handled = actions.escape?.(event);
      lastEscapeAt = handled ? 0 : now;
      if (handled) event.preventDefault();
      return;
    }
    if (event.altKey && event.key === "ArrowDown") return invoke(event, () => actions.switchSession?.(1));
    if (event.altKey && event.key === "ArrowUp") return invoke(event, () => actions.switchSession?.(-1));
  };
  document.addEventListener("keydown", handler);
  return () => document.removeEventListener("keydown", handler);
}

function invoke(event, action) {
  if (!action) return;
  event.preventDefault();
  action();
}
