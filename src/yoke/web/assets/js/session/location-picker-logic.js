export function createLocationBrowseCoordinator() {
  let generation = 0;
  return {
    invalidate() {
      generation += 1;
    },
    async run(load, onSuccess, onError) {
      const requestGeneration = ++generation;
      try {
        const value = await load();
        if (requestGeneration !== generation) return false;
        onSuccess(value);
        return true;
      } catch (error) {
        if (requestGeneration !== generation) return false;
        onError(error);
        return false;
      }
    },
  };
}

export function isLocationBrowseQuery(value) {
  const query = String(value || "").trim();
  return query === "~"
    || query.startsWith("~/")
    || query.startsWith("/")
    || /^[A-Za-z]:[\\/]/.test(query)
    || query.startsWith("\\\\");
}

export function withTrailingSeparator(directory, separator) {
  if (!directory) return directory;
  if (directory.endsWith("/") || directory.endsWith("\\")) return directory;
  return `${directory}${separator}`;
}

export function lastLocationPath(directory) {
  const parts = String(directory || "").split(/[\\/]+/).filter(Boolean);
  return parts.at(-1) || directory || "Location";
}
