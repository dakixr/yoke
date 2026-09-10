export function matchesSearch(query, ...values) {
  const needle = String(query || "").trim().toLowerCase();
  return !needle || values.some((value) => String(value || "").toLowerCase().includes(needle));
}

export function groupTools(tools, search, filter, feedback = {}) {
  const groups = new Map();
  for (const tool of tools) {
    const keepFeedback = feedback[tool.name]?.pending || feedback[tool.name]?.error;
    if (!keepFeedback && (!matchesSearch(search, tool.name, tool.description, tool.source, tool.capabilityID)
      || (filter === "enabled" && !tool.enabled) || (filter === "disabled" && tool.enabled))) continue;
    const source = tool.source || "Unknown source";
    if (!groups.has(source)) groups.set(source, []);
    groups.get(source).push(tool);
  }
  return [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([source, items]) => ({
    source, tools: [...items].sort((a, b) => a.name.localeCompare(b.name)),
  }));
}

export function skillSections(skills, search) {
  const active = new Map((skills.active || []).map((skill) => [skill.name, skill]));
  const available = new Map((skills.available || []).map((skill) => [skill.name, skill]));
  return {
    active: [...active.values()].map((skill) => ({ ...available.get(skill.name), ...skill }))
      .sort((a, b) => a.name.localeCompare(b.name)),
    available: [...available.values()].filter((skill) => !active.has(skill.name)
      && matchesSearch(search, skill.name, skill.description))
      .sort((a, b) => a.name.localeCompare(b.name)),
  };
}

// The denylist takes precedence, including when an allowlist is present.
export function mcpToolEnabled(server, name) {
  return (!Array.isArray(server.enabledTools) || server.enabledTools.includes(name))
    && !(server.disabledTools || []).includes(name);
}

export function mcpToolPatch(server, name, enabled) {
  const disabled = new Set(server.disabledTools || []);
  if (enabled) disabled.delete(name); else disabled.add(name);
  const patch = { disabledTools: [...disabled] };
  if (Array.isArray(server.enabledTools)) {
    const allowed = new Set(server.enabledTools);
    if (enabled) allowed.add(name); else allowed.delete(name);
    patch.enabledTools = [...allowed];
  }
  return patch;
}

export function mcpCounts(server) {
  const tools = server.tools || [];
  return { total: tools.length, enabled: server.enabled ? tools.filter((tool) => mcpToolEnabled(server, tool.name)).length : 0 };
}

export function matchingLines(content, query) {
  if (!query) return [];
  const needle = query.toLowerCase();
  return String(content).split("\n").flatMap((line, index) => line.toLowerCase().includes(needle) ? [index] : []);
}

export function messagePreview(message) {
  return (message.content || []).map((part) => part.type === "text" ? part.text : `[${part.type || "non-text"} block]`)
    .join(" ").replace(/\s+/g, " ").slice(0, 200) || "No content blocks";
}
