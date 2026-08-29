// @ts-check

export function installKeybindings(actions) {
  let lastEscapeAt = 0;
  let ctrlXAt = 0;
  const handler = (event) => {
    const command = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (event.key !== "Escape") lastEscapeAt = 0;
    if (event.ctrlKey && !event.metaKey && key === "x") {
      ctrlXAt = performance.now();
      event.preventDefault();
      return;
    }
    if (ctrlXAt && ["control", "shift", "alt", "meta"].includes(key)) return;
    if (ctrlXAt) {
      const active = performance.now() - ctrlXAt <= 1200;
      ctrlXAt = 0;
      if (active) {
        if (key === "o") return invoke(event, actions.toolInspector);
        if (event.ctrlKey && key === "p") return invoke(event, actions.processInspector);
        if (key === "q") return invoke(event, actions.queueManager);
        if (key === "m") return invoke(event, actions.modelSelector);
        if (key === "t") return invoke(event, actions.sessionTree);
      }
    }
    if (command && event.shiftKey && !event.altKey && key === "o") return invoke(event, actions.newSession);
    if (command && key === "k") return invoke(event, actions.palette);
    if (command && key === "n") return invoke(event, actions.newSession);
    if (command && key === "b") return invoke(event, actions.toggleSidebar);
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
