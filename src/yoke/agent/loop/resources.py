"""Shared lifetime management for resources owned by local tools."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from yoke.agent.tools import LocalTool

_RESOURCE_LEASES: dict[int, tuple[object, int]] = {}
_RESOURCE_LEASE_LOCK = threading.RLock()


def acquire_tool_resources(tools: Iterable[LocalTool]) -> None:
    """Acquire one runtime lease for each distinct tool-owned resource."""
    with _RESOURCE_LEASE_LOCK:
        for resource in _tool_resources(tools):
            resource_id = id(resource)
            current = _RESOURCE_LEASES.get(resource_id)
            if current is None:
                _RESOURCE_LEASES[resource_id] = (resource, 1)
            else:
                _RESOURCE_LEASES[resource_id] = (current[0], current[1] + 1)


def release_tool_resources(tools: Iterable[LocalTool]) -> None:
    """Release leases and close resources after their final runtime releases."""
    resources = _tool_resources(tools)
    ready_to_close: list[object] = []
    with _RESOURCE_LEASE_LOCK:
        for resource in resources:
            resource_id = id(resource)
            current = _RESOURCE_LEASES.get(resource_id)
            if current is None:
                continue
            if current[1] > 1:
                _RESOURCE_LEASES[resource_id] = (current[0], current[1] - 1)
                continue
            del _RESOURCE_LEASES[resource_id]
            ready_to_close.append(current[0])

    first_error: BaseException | None = None
    for resource in ready_to_close:
        close = getattr(resource, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error


def _tool_resources(tools: Iterable[LocalTool]) -> list[object]:
    resources: list[object] = []
    seen: set[int] = set()
    for tool in tools:
        for resource in tool.owned_resources():
            resource_id = id(resource)
            if resource_id in seen:
                continue
            seen.add(resource_id)
            resources.append(resource)
    return resources
