// @ts-check

export function currentRoute() {
  const path = window.location.pathname;
  if (path === "/new") {
    return { name: "new", draftID: new URLSearchParams(location.search).get("draft") };
  }
  const match = path.match(/^\/session\/([^/]+)$/);
  if (match) return { name: "session", sessionID: decodeURIComponent(match[1]) };
  if (path === "/settings") return { name: "settings" };
  return { name: "home" };
}

export function navigate(path, { replace = false } = {}) {
  const method = replace ? "replaceState" : "pushState";
  history[method]({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function sessionPath(sessionID) {
  return `/session/${encodeURIComponent(sessionID)}`;
}

export function draftPath(draftID) {
  return `/new?draft=${encodeURIComponent(draftID)}`;
}
