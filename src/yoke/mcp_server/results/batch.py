"""Keep per-item outcomes when reducing a batch to its wire budget."""

from __future__ import annotations

import json
from typing import Any

from yoke.mcp_server.results.store import ResultStore


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True))


def minimum_budget(ids: list[str]) -> int:
    # Include escaped Unicode IDs, not Python character counts. Reserve room
    # for every outcome and a retained payload receipt before any work starts.
    return 1024 + sum(512 + encoded_size(item_id) for item_id in ids)


def project_batch(
    result: dict[str, Any], store: ResultStore, budget: int
) -> dict[str, Any]:
    if encoded_size(result) <= budget:
        return result
    original = result["items"]
    items = [{"id": item["id"], "status": item["status"]} for item in original]
    projected = {**result, "items": items}
    remaining = budget - encoded_size(projected)
    for index, item in enumerate(original):
        allowance = remaining // (len(items) - index)
        base = items[index]
        extra = encoded_size(item) - encoded_size(base)
        if extra <= allowance:
            items[index] = item
        else:
            if allowance < 256:
                raise ValueError("Batch budget cannot retain every item outcome")
            payload = {
                key: value for key, value in item.items() if key not in {"id", "status"}
            }
            if set(payload) == {"data"} and isinstance(payload["data"], dict):
                payload = payload["data"]
            try:
                items[index] = {
                    **base,
                    "data": store.project(payload, limit=allowance - 16),
                }
            except ValueError:
                items[index] = {
                    **base,
                    "status": "error",
                    "error": "Result could not be retained; narrow the request",
                }
        remaining -= encoded_size(items[index]) - encoded_size(base)
    if encoded_size(projected) > budget:
        raise ValueError("Batch result exceeds its wire budget")
    return projected
