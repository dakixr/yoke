// @ts-check

const DRAFTS_KEY = "yoke.web.drafts.v1";
const SESSION_COMPOSER_DRAFTS_KEY = "yoke.web.sessionComposerDrafts.v1";
const REVIEWED_KEY = "yoke.web.reviewed.v1";
const DONE_KEY = "yoke.web.done.v1";
const SCROLL_KEY = "yoke.web.scroll.v1";
const TOKEN_KEY = "yoke.web.token";

function readJSON(storage, key, fallback) {
  try {
    const raw = storage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

export function readDrafts() {
  return readJSON(localStorage, DRAFTS_KEY, {});
}

export function writeDrafts(value) {
  localStorage.setItem(DRAFTS_KEY, JSON.stringify(value));
}

export function readSessionComposerDrafts() {
  return readJSON(sessionStorage, SESSION_COMPOSER_DRAFTS_KEY, {});
}

export function writeSessionComposerDrafts(value) {
  sessionStorage.setItem(SESSION_COMPOSER_DRAFTS_KEY, JSON.stringify(value));
}

export function readReviewed() {
  return readJSON(localStorage, REVIEWED_KEY, {});
}

export function writeReviewed(value) {
  localStorage.setItem(REVIEWED_KEY, JSON.stringify(value));
}

export function readDone() {
  return readJSON(localStorage, DONE_KEY, {});
}

export function writeDone(value) {
  localStorage.setItem(DONE_KEY, JSON.stringify(value));
}

export function getScroll(sessionID) {
  return Number(readJSON(sessionStorage, SCROLL_KEY, {})[sessionID] || 0);
}

export function setScroll(sessionID, value) {
  const data = readJSON(sessionStorage, SCROLL_KEY, {});
  data[sessionID] = Math.max(0, Math.round(value));
  sessionStorage.setItem(SCROLL_KEY, JSON.stringify(data));
}

export function readToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function writeToken(token) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}
