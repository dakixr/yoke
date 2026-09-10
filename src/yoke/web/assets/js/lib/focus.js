export function trapFocus(container, event) {
  if (!container || event.key !== "Tab" || event.defaultPrevented) return;
  const nodes = focusableElements(container);
  if (!nodes.length) {
    event.preventDefault();
    container.focus();
    return;
  }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !nodes.includes(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !container.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

export function focusableElements(container) {
  return [...container.querySelectorAll(
    'button, input, textarea, select, summary, [href], [tabindex]',
  )].filter((node) => node.tabIndex >= 0 && !node.matches(":disabled")
    && !node.closest("[inert]") && !node.hidden && node.getClientRects().length > 0);
}
