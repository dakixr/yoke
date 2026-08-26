export function workingDuration(startedAt, now = Date.now()) {
  if (!startedAt) return "Working";
  const start = Date.parse(startedAt);
  if (!Number.isFinite(start)) return "Working";
  const seconds = Math.max(0, Math.floor((now - start) / 1000));
  if (seconds < 60) return `Working ${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `Working ${minutes}m${String(rest).padStart(2, "0")}s`;
}

export function shortAge(value) {
  if (!value) return "";
  const delta = Math.max(0, Date.now() - Date.parse(value));
  const minutes = Math.floor(delta / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}
