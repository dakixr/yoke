import assert from "node:assert/strict";
import {
  appendBrowsePathSegment,
  canNavigateUp,
  filterLocationBrowseEntries,
  filterRecentLocations,
  getBrowseDirectoryPath,
  getBrowseLeafPathSegment,
  getBrowseParentPath,
  getLocationBrowsePath,
  hasTrailingPathSeparator,
  isFilesystemBrowseQuery,
  lastLocationPath,
} from "../src/yoke/web/assets/js/session/location-picker-logic.js";

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const entry = (name) => ({ name, directory: `/home/dev/${name}` });

test("only rooted text addresses the filesystem", () => {
  assert.equal(isFilesystemBrowseQuery("/home"), true);
  assert.equal(isFilesystemBrowseQuery("~/dev"), true);
  assert.equal(isFilesystemBrowseQuery("C:\\code"), true);
  assert.equal(isFilesystemBrowseQuery("\\\\server\\share"), true);
  // A bare tilde is a project search, the way T3 Code treats it.
  assert.equal(isFilesystemBrowseQuery("~"), false);
  assert.equal(isFilesystemBrowseQuery("yoke"), false);
  assert.equal(isFilesystemBrowseQuery(""), false);
});

test("the trailing separator decides directory from partial leaf", () => {
  assert.equal(hasTrailingPathSeparator("/home/dev/"), true);
  assert.equal(hasTrailingPathSeparator("/home/dev"), false);
  assert.equal(getBrowseDirectoryPath("/home/dev/"), "/home/dev/");
  assert.equal(getBrowseDirectoryPath("/home/dev/yo"), "/home/dev/");
  assert.equal(getBrowseDirectoryPath("/home"), "/");
  assert.equal(getBrowseLeafPathSegment("/home/dev/yo"), "yo");
  assert.equal(getBrowseLeafPathSegment("/home/dev/"), "");
});

test("a query resolves to the directory to list and the leaf to filter", () => {
  assert.deepEqual(getLocationBrowsePath("/home/dev/yo"), {
    isBrowsing: true,
    directoryPath: "/home/dev/",
    filterQuery: "yo",
    parentPath: "/home/",
    canBrowseUp: true,
  });
  assert.deepEqual(getLocationBrowsePath("/home/dev/"), {
    isBrowsing: true,
    directoryPath: "/home/dev/",
    filterQuery: "",
    parentPath: "/home/",
    canBrowseUp: true,
  });
  assert.equal(getLocationBrowsePath("yoke").isBrowsing, false);
});

test("the filesystem root cannot be navigated above", () => {
  assert.equal(getBrowseParentPath("/"), null);
  assert.equal(canNavigateUp("/"), false);
  assert.equal(getBrowseParentPath("/home/"), "/");
  assert.equal(canNavigateUp("/home/"), true);
  // Only a directory path can go up; a partial leaf is still being typed.
  assert.equal(canNavigateUp("/home/de"), false);
});

test("opening a folder appends its segment and a separator", () => {
  assert.equal(appendBrowsePathSegment("/home/dev/yo", "yoke"), "/home/dev/yoke/");
  assert.equal(appendBrowsePathSegment("/home/dev/", "yoke"), "/home/dev/yoke/");
});

test("windows drives and UNC shares keep their own separator", () => {
  assert.equal(appendBrowsePathSegment("C:\\code\\", "yoke"), "C:\\code\\yoke\\");
  assert.equal(getBrowseParentPath("C:\\code\\yoke\\"), "C:\\code\\");
  assert.equal(getBrowseParentPath("C:\\code\\"), "C:\\");
  assert.equal(getBrowseParentPath("\\\\server\\share\\code\\"), "\\\\server\\share\\");
  assert.equal(lastLocationPath("C:\\code\\yoke"), "yoke");
});

test("listings filter by prefix and hide dot directories until asked", () => {
  const entries = [entry(".config"), entry("docs"), entry("yoke"), entry("yoke-notes")];
  assert.deepEqual(
    filterLocationBrowseEntries(entries, "").visibleEntries.map((item) => item.name),
    ["docs", "yoke", "yoke-notes"],
  );
  assert.deepEqual(
    filterLocationBrowseEntries(entries, "yoke").visibleEntries.map((item) => item.name),
    ["yoke", "yoke-notes"],
  );
  assert.deepEqual(
    filterLocationBrowseEntries(entries, ".").visibleEntries.map((item) => item.name),
    [".config"],
  );
  // Prefix only: a substring never matches, matching T3 Code's listing filter.
  assert.deepEqual(filterLocationBrowseEntries(entries, "ok").visibleEntries, []);
  assert.equal(filterLocationBrowseEntries(entries, "yoke").exactEntry.name, "yoke");
  assert.equal(filterLocationBrowseEntries(entries, "yok").exactEntry, null);
});

test("a non-path query searches known projects", () => {
  const recents = [
    { directory: "/home/dev/site-builder" },
    { directory: "/home/dev/yoke" },
  ];
  assert.deepEqual(
    filterRecentLocations(recents, "yoke").map((item) => item.directory),
    ["/home/dev/yoke"],
  );
  assert.equal(filterRecentLocations(recents, "").length, 2);
  assert.equal(filterRecentLocations(recents, "zzz").length, 0);
});

for (const { name, fn } of tests) {
  fn();
  console.log(`PASS ${name}`);
}
console.log(JSON.stringify({ tests: tests.length }));
