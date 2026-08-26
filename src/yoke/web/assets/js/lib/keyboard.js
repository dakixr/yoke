// @ts-check

export function installKeybindings(actions) {
  const handler = (event) => {
    const command = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (command && key === "k") return invoke(event, actions.palette);
    if (command && key === "n") return invoke(event, actions.newSession);
    if (command && key === "b") return invoke(event, actions.toggleSidebar);
    if (event.key === "Escape") return actions.escape?.(event);
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
