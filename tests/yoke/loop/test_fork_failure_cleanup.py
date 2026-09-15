from __future__ import annotations

from pathlib import Path

import pytest

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import ReadTool, ToolRegistrationContext, ToolRegistrationResult


class ClosingProvider:
    def __init__(self) -> None:
        self.closed = 0
        self.forks: list[ClosingProvider] = []

    def complete(self, messages, tools) -> Message:
        return Message.assistant("done")

    def fork_for_turn(self) -> ClosingProvider:
        fork = ClosingProvider()
        self.forks.append(fork)
        return fork

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("isolated", [True, False])
def test_fork_binding_failure_closes_only_the_unreturned_isolated_provider(
    tmp_path: Path, isolated: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = ClosingProvider()

    def tools(context: ToolRegistrationContext) -> ToolRegistrationResult:
        return ToolRegistrationResult(tools=[ReadTool.bind(root=context.root)])

    parent = RuntimeAgent(provider, [], tool_root=root, tool_factory=tools)
    try:
        root.rmdir()
        with pytest.raises(ValueError, match="Workspace root"):
            parent.fork(isolate_provider=isolated)
        assert not parent.closed
        assert provider.closed == 0
        assert [fork.closed for fork in provider.forks] == ([1] if isolated else [])
        root.mkdir()
        successful = parent.fork(isolate_provider=isolated)
        successful.close()
        if isolated:
            provider.forks[-1].close()
        assert provider.closed == 0
    finally:
        parent.close()
        provider.close()


def test_fork_failure_after_construction_releases_agent_and_provider(
    tmp_path, monkeypatch
):
    provider = ClosingProvider()
    parent = RuntimeAgent(
        provider, [], tool_root=tmp_path, messages=[Message.user("old")]
    )
    try:

        def fail_sync(self, context):
            raise RuntimeError("projection failed")

        monkeypatch.setattr(RuntimeAgent, "_sync_context_instructions", fail_sync)
        with pytest.raises(RuntimeError, match="projection failed"):
            parent.fork(isolate_provider=True)
        assert provider.forks[0].closed == 1
        assert provider.closed == 0
        assert not parent.closed
    finally:
        parent.close()
        provider.close()
