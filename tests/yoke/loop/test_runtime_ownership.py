"""Runtime ownership must preserve shared-resource and detached-worker lifetimes."""

from __future__ import annotations

import threading

from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.in_process_tool import execute_in_process_tool
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool


class Resource:
    """Record the number of final releases."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        """Observe final release."""
        self.closed += 1


class ResourceTool(LocalTool):
    """Own a resource without running subprocesses."""

    name = "resource"
    description = "Hold a shared resource."
    execute_in_process = True

    def owned_resources(self) -> tuple[object, ...]:
        """Share the explicitly supplied resource."""
        return (self._context["resource"],)

    def execute(self) -> dict[str, object]:
        """Return immediately unless the test supplies a release event."""
        release = self._context.get("release")
        if isinstance(release, threading.Event):
            release.wait(timeout=5)
        return {"ok": True}


class Provider:
    """A provider that never needs transport resources."""

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        """Finish the turn."""
        return Message.assistant("done")


def test_independent_runtimes_keep_shared_resource_until_final_close() -> None:
    """Independent bindings of one resource must share its final-release rule."""
    resource = Resource()
    first = RuntimeAgent(Provider(), [ResourceTool.bind(resource=resource)])
    second = RuntimeAgent(Provider(), [ResourceTool.bind(resource=resource)])
    fork = first.fork()
    try:
        first.close()
        assert resource.closed == 0
        second.close()
        assert resource.closed == 0
        fork.close()
        assert resource.closed == 1
    finally:
        first.close()
        second.close()
        fork.close()


def test_closing_fork_does_not_cancel_the_primary_worker() -> None:
    """Forks share resource leases while keeping distinct worker groups."""
    release = threading.Event()
    resource = Resource()
    primary = RuntimeAgent(
        Provider(), [ResourceTool.bind(resource=resource, release=release)]
    )
    fork = primary.fork()
    try:
        _, stopped = execute_in_process_tool(
            tools=primary.tools,
            name="resource",
            arguments={},
            stop_requested=lambda: True,
        )
        assert stopped
        fork.close()
        assert primary.closed is False
        assert resource.closed == 0
        release.set()
        primary.close()
        assert resource.closed == 1
    finally:
        release.set()
        primary.close()
        fork.close()
