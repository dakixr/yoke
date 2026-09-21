// Path handling ported from T3 Code's project picker
// (packages/client-runtime/src/state/projects.ts and state/filesystem.ts) so
// that both products browse a filesystem the same way.

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

function isWindowsDrivePath(value) {
  return /^[A-Za-z]:[\\/]/.test(value);
}

function isUncPath(value) {
  return value.startsWith("\\\\");
}

function absolutePathKind(value) {
  if (isWindowsDrivePath(value) || isUncPath(value)) return "windows";
  if (value.startsWith("/")) return "unix";
  return null;
}

function preferredPathSeparator(value) {
  const kind = absolutePathKind(value);
  if (kind === "windows") return "\\";
  if (kind === "unix") return "/";
  return value.includes("\\") ? "\\" : "/";
}

export function hasTrailingPathSeparator(value) {
  return (absolutePathKind(value) === "unix" ? /\/$/ : /[\\/]$/).test(value);
}

function splitPathSegments(value, separator) {
  return value.split(separator === "/" ? /\/+/ : /[\\/]+/).filter(Boolean);
}

function lastPathSeparatorIndex(value) {
  if (absolutePathKind(value) === "unix") return value.lastIndexOf("/");
  return Math.max(value.lastIndexOf("/"), value.lastIndexOf("\\"));
}

function splitAbsolutePath(value) {
  if (isWindowsDrivePath(value)) {
    const root = `${value.slice(0, 2)}\\`;
    return { root, separator: "\\", segments: splitPathSegments(value.slice(root.length), "\\") };
  }
  if (isUncPath(value)) {
    const [server, share, ...rest] = splitPathSegments(value, "\\");
    if (!server || !share) return null;
    return { root: `\\\\${server}\\${share}\\`, separator: "\\", segments: rest };
  }
  if (value.startsWith("/")) {
    return { root: "/", separator: "/", segments: splitPathSegments(value.slice(1), "/") };
  }
  return null;
}

/** Whether the typed text addresses the filesystem rather than naming a project. */
export function isFilesystemBrowseQuery(value) {
  const query = String(value || "");
  return query.startsWith("/")
    || query.startsWith("~/")
    || isWindowsDrivePath(query)
    || isUncPath(query);
}

export function appendBrowsePathSegment(currentPath, segment) {
  const separator = preferredPathSeparator(currentPath);
  return `${getBrowseDirectoryPath(currentPath)}${segment}${separator}`;
}

/** The part of the typed text that filters the listing, "" when it is a directory. */
export function getBrowseLeafPathSegment(currentPath) {
  return currentPath.slice(lastPathSeparatorIndex(currentPath) + 1);
}

/** The directory the typed text addresses, dropping any partial leaf segment. */
export function getBrowseDirectoryPath(currentPath) {
  if (hasTrailingPathSeparator(currentPath)) return currentPath;
  const index = lastPathSeparatorIndex(currentPath);
  return index < 0 ? currentPath : currentPath.slice(0, index + 1);
}

export function ensureBrowseDirectoryPath(currentPath) {
  const trimmed = String(currentPath || "").trim();
  if (!trimmed.length || hasTrailingPathSeparator(trimmed)) return trimmed;
  return `${trimmed}${preferredPathSeparator(trimmed)}`;
}

export function getBrowseParentPath(currentPath) {
  const trimmed = String(currentPath || "").trim();
  const absolute = splitAbsolutePath(trimmed);
  if (absolute) {
    if (!absolute.segments.length) return null;
    if (absolute.segments.length === 1) return absolute.root;
    const parent = absolute.segments.slice(0, -1).join(absolute.separator);
    return `${absolute.root}${parent}${absolute.separator}`;
  }
  const separator = preferredPathSeparator(trimmed);
  const index = lastPathSeparatorIndex(trimmed);
  if (index < 0) return null;
  if (index === 2 && /^[A-Za-z]:/.test(trimmed)) return `${trimmed.slice(0, 2)}${separator}`;
  return trimmed.slice(0, index + 1);
}

export function canNavigateUp(currentPath) {
  return hasTrailingPathSeparator(currentPath) && getBrowseParentPath(currentPath) !== null;
}

export function lastLocationPath(directory) {
  const value = String(directory || "");
  const absolute = splitAbsolutePath(value);
  if (absolute) return absolute.segments.at(-1) || value;
  return value.split(/[\\/]+/).filter(Boolean).at(-1) || value || "Location";
}

/** Derive what a typed query addresses: the directory to list and the leaf filter. */
export function getLocationBrowsePath(query) {
  const isBrowsing = isFilesystemBrowseQuery(query);
  const directoryPath = isBrowsing ? getBrowseDirectoryPath(query) : "";
  const filterQuery = isBrowsing && !hasTrailingPathSeparator(query)
    ? getBrowseLeafPathSegment(query)
    : "";
  return {
    isBrowsing,
    directoryPath,
    filterQuery,
    parentPath: isBrowsing ? getBrowseParentPath(directoryPath) : null,
    canBrowseUp: isBrowsing && canNavigateUp(directoryPath),
  };
}

/** Prefix-filter a directory listing, revealing hidden entries only on a dot query. */
export function filterLocationBrowseEntries(entries, query) {
  const lowerQuery = String(query || "").toLowerCase();
  const showHidden = lowerQuery.startsWith(".");
  const visibleEntries = (entries || []).filter(
    (entry) => entry.name.toLowerCase().startsWith(lowerQuery)
      && (showHidden || !entry.name.startsWith(".")),
  );
  const exactEntry = lowerQuery.length
    ? visibleEntries.find((entry) => entry.name === query) || null
    : null;
  return { visibleEntries, exactEntry };
}

/** Rank known projects for a non-path query the way a command palette would. */
export function filterRecentLocations(recentLocations, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return [...(recentLocations || [])];
  return (recentLocations || []).filter((item) => {
    const directory = String(item.directory || "").toLowerCase();
    return directory.includes(needle) || lastLocationPath(directory).includes(needle);
  });
}
