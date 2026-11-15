"""Runtime-owned leases for resources shared by local tools."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from weakref import WeakSet

from yoke.agent.tools import LocalTool


class ToolResources:
    """Own runtime leases; close a resource after its final owner releases it.

    The weak owner directory coordinates independently constructed runtimes too.
    It never retains a runtime or resource. All resource references and counts
    live on the owning runtime's lease collection.
    """

    _owners: WeakSet[ToolResources] = WeakSet()
    lock = threading.RLock()

    def __init__(self) -> None:
        self._leases: dict[int, tuple[object, int]] = {}
        with self.lock:
            self._owners.add(self)

    def acquire(self, tools: Iterable[LocalTool]) -> None:
        """Acquire one lease for each distinct tool-owned resource."""
        with self.lock:
            for resource in _tool_resources(tools):
                resource_id = id(resource)
                current = self._leases.get(resource_id)
                self._leases[resource_id] = (
                    resource,
                    1 if current is None else current[1] + 1,
                )

    def release(self, tools: Iterable[LocalTool]) -> None:
        """Release leases, closing every final resource outside the lease lock."""
        resources = _tool_resources(tools)
        ready_to_close: list[object] = []
        with self.lock:
            for resource in resources:
                resource_id = id(resource)
                current = self._leases.get(resource_id)
                if current is None:
                    continue
                if current[1] > 1:
                    self._leases[resource_id] = (current[0], current[1] - 1)
                    continue
                del self._leases[resource_id]
                if not any(resource_id in owner._leases for owner in self._owners):
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
