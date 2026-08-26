// @ts-check

export class ApiError extends Error {
  constructor(status, code, message, details = null, requestID = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestID = requestID;
  }
}

function queryString(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const rendered = query.toString();
  return rendered ? `?${rendered}` : "";
}

export class YokeApi {
  constructor() {
    this.token = null;
  }

  setToken(token) {
    this.token = token || null;
  }

  headers(extra = {}) {
    const headers = { "X-Request-ID": crypto.randomUUID(), ...extra };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    return headers;
  }

  async request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: this.headers(options.headers || {}),
    });
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch { /* response may be plain text */ }
      const error = payload?.error || {};
      throw new ApiError(
        response.status,
        error.code || "http_error",
        error.message || `${response.status} ${response.statusText}`,
        error.details || null,
        error.requestID || response.headers.get("x-request-id"),
      );
    }
    if (response.status === 204) return null;
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) return response.json();
    return response.text();
  }

  json(path, method, body) {
    return this.request(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  capabilities() { return this.request("/api/v1/capabilities"); }
  commands() { return this.request("/api/v1/command"); }
  activeSessions() { return this.request("/api/v1/session/active"); }
  recentLocations() { return this.request("/api/v1/location/recent"); }
  resolveLocation(directory) {
    return this.request(`/api/v1/location${queryString({ directory })}`);
  }
  providers(directory) {
    return this.request(`/api/v1/provider${queryString({ directory })}`);
  }
  models({ directory, provider, search } = {}) {
    return this.request(`/api/v1/model${queryString({ directory, provider, search })}`);
  }
  listSessions({ directory, search, pinned, archived, limit = 50, order = "updatedDesc", cursor } = {}) {
    return this.request(`/api/v1/session${queryString({ directory, search, pinned, archived, limit, order, cursor })}`);
  }
  getSession(id) { return this.request(`/api/v1/session/${encodeURIComponent(id)}`); }
  createSession(body) { return this.json("/api/v1/session", "POST", body); }
  patchSession(id, body) { return this.json(`/api/v1/session/${encodeURIComponent(id)}`, "PATCH", body); }
  forkSession(id, body = {}) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/fork`, "POST", body); }
  selectModel(id, body) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/selection`, "POST", body); }
  compact(id) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/compact`, "POST", { reason: "manual" }); }
  messages(id, { limit = 100, order = "desc", cursor } = {}) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/message${queryString({ limit, order, cursor, branch: "active" })}`);
  }
  context(id, { includeSystem = false, includeToolResults = true } = {}) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/context${queryString({ includeSystem, includeToolResults })}`);
  }
  history(id, after = 0, limit = 200) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/history${queryString({ after, limit })}`);
  }
  admitPrompt(id, body) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/prompt`, "POST", body); }
  interrupt(id) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/interrupt`, "POST", {}); }
  queue(id) { return this.request(`/api/v1/session/${encodeURIComponent(id)}/queue`); }
  patchQueue(id, body) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/queue`, "PATCH", body); }
  tree(id) { return this.request(`/api/v1/session/${encodeURIComponent(id)}/tree`); }
  treePreview(id, targetID) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/tree/navigation-preview${queryString({ targetID, includeAbandoned: true })}`);
  }
  navigateTree(id, body) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/tree/navigate`, "POST", body); }
  patchTreeEntry(id, entryID, body) {
    return this.json(`/api/v1/session/${encodeURIComponent(id)}/tree/${encodeURIComponent(entryID)}`, "PATCH", body);
  }
  permissions(id) { return this.request(`/api/v1/session/${encodeURIComponent(id)}/permission`); }
  replyPermission(id, requestID, body) {
    return this.json(`/api/v1/session/${encodeURIComponent(id)}/permission/${encodeURIComponent(requestID)}/reply`, "POST", body);
  }
  questions(id) { return this.request(`/api/v1/session/${encodeURIComponent(id)}/question`); }
  replyQuestion(id, requestID, answers) {
    return this.json(`/api/v1/session/${encodeURIComponent(id)}/question/${encodeURIComponent(requestID)}/reply`, "POST", { answers });
  }
  rejectQuestion(id, requestID) {
    return this.json(`/api/v1/session/${encodeURIComponent(id)}/question/${encodeURIComponent(requestID)}/reject`, "POST", {});
  }
  async upload(file, sessionID = null) {
    const form = new FormData();
    form.append("file", file, file.name);
    return this.request(`/api/v1/upload${queryString({ sessionID, purpose: "promptAttachment" })}`, {
      method: "POST",
      body: form,
    });
  }
  tools({ directory, sessionID } = {}) {
    return this.request(`/api/v1/tool${queryString({ directory, sessionID })}`);
  }
  patchTools(id, body) { return this.json(`/api/v1/session/${encodeURIComponent(id)}/tool`, "PATCH", body); }
  skills({ directory, search } = {}) {
    return this.request(`/api/v1/skill${queryString({ directory, search })}`);
  }
  sessionSkills(id) { return this.request(`/api/v1/session/${encodeURIComponent(id)}/skill`); }
  activateSkill(id, name, prompt = null) {
    return this.json(`/api/v1/session/${encodeURIComponent(id)}/skill/${encodeURIComponent(name)}/activate`, "POST", { prompt });
  }
  mcp({ directory, includeTools = false } = {}) {
    return this.request(`/api/v1/mcp${queryString({ directory, includeTools })}`);
  }
  sessionMcp(id, includeTools = true) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/mcp${queryString({ includeTools })}`);
  }
  patchMcp(id, server, body) {
    return this.json(`/api/v1/session/${encodeURIComponent(id)}/mcp/${encodeURIComponent(server)}`, "PATCH", body);
  }
  toolCalls(id, { status, turnID, limit = 100, cursor } = {}) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/tool-call${queryString({ status, turnID, limit, cursor })}`);
  }
  toolCall(id, callID) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/tool-call/${encodeURIComponent(callID)}`);
  }
  toolOutput(id, callID, afterSeq = 0, limit = 200) {
    return this.request(`/api/v1/session/${encodeURIComponent(id)}/tool-call/${encodeURIComponent(callID)}/output${queryString({ afterSeq, limit })}`);
  }
  processes({ sessionID, status, limit = 100 } = {}) {
    return this.request(`/api/v1/process${queryString({ sessionID, status, limit })}`);
  }
  process(id) { return this.request(`/api/v1/process/${encodeURIComponent(id)}`); }
  processOutput(id, afterSeq = 0, limit = 200) {
    return this.request(`/api/v1/process/${encodeURIComponent(id)}/output${queryString({ afterSeq, limit })}`);
  }
  processStdin(id, text) { return this.json(`/api/v1/process/${encodeURIComponent(id)}/stdin`, "POST", { text }); }
  processSignal(id, signal) { return this.json(`/api/v1/process/${encodeURIComponent(id)}/signal`, "POST", { signal }); }
  fsList(directory, path = null) {
    return this.request(`/api/v1/fs/list${queryString({ directory, path })}`);
  }
  fsFind(directory, query, type = "file", limit = 50) {
    return this.request(`/api/v1/fs/find${queryString({ directory, query, type, limit })}`);
  }
  fsRead(directory, path) {
    return this.request(`/api/v1/fs/read${queryString({ directory, path })}`);
  }
}

export const api = new YokeApi();
