export function filterModelChoices(models, query = "", providers = []) {
  const providerMap = new Map(providers.map((item) => [item.id, item]));
  const needle = String(query || "").trim().toLowerCase();
  return (models || [])
    .filter((item) => {
      if (!needle) return true;
      return [item.provider, item.id, item.name]
        .some((value) => String(value || "").toLowerCase().includes(needle));
    })
    .map((item) => ({
      ...item,
      providerReady: providerMap.get(item.provider)?.ready !== false,
    }));
}

export function groupModelChoices(models) {
  const groups = [];
  let current = null;
  for (const model of models || []) {
    if (!current || current.provider !== model.provider) {
      current = { provider: model.provider, models: [] };
      groups.push(current);
    }
    current.models.push(model);
  }
  return groups;
}

export function resolveModelEffort(model, currentEffort = "", providerEffort = "") {
  const values = (model?.reasoningEfforts || []).filter(Boolean);
  if (!values.length) return "";
  if (values.includes(currentEffort)) return currentEffort;
  if (values.includes(providerEffort)) return providerEffort;
  return values[0];
}

export function modelSelectionErrorMessage(error) {
  const message = error?.message || String(error);
  if (error?.code === "model_context_too_small") return message;
  return `Could not change the model. ${message}`;
}

export function modelNavigationIndex(index, count, key, pageSize = 5) {
  if (!count) return 0;
  const current = Math.max(0, Math.min(index, count - 1));
  if (key === "ArrowDown") return (current + 1) % count;
  if (key === "ArrowUp") return (current - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  if (key === "PageDown") return Math.min(count - 1, current + pageSize);
  if (key === "PageUp") return Math.max(0, current - pageSize);
  return current;
}

export function formatContextWindow(tokens) {
  if (!Number.isFinite(tokens)) return "";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens >= 10_000_000 ? 0 : 1)}m ctx`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k ctx`;
  return `${tokens} ctx`;
}
