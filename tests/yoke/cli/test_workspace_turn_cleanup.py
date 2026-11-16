"""Unreturned CLI forks retain leases until their resources are closed."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

from pathlib import Path
from threading import Event, Lock
from unittest.mock import Mock

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import ReadTool, ToolRegistrationContext, ToolRegistrationResult
from yoke.cli.interactive.common import PromptCliState, TurnFailure
from yoke.cli.interactive.prompt import turns
from yoke.session.workspace import WorkspaceBusy, workspace_lease

from .terminal_daemon_support import terminal as terminal


class ClosingProvider:
    def __init__(self) -> None:
        self.closed = 0
        self.is_turn = False
        self.forks: list[ClosingProvider] = []
        self.before_fork = lambda: None
        self.before_close = lambda: None

    def complete(self, messages, tools) -> Message:
        raise AssertionError("No model calls are allowed")

    def fork_for_turn(self) -> ClosingProvider:
        self.before_fork()
        fork = ClosingProvider()
        fork.is_turn = True
        fork.before_close = self.before_close
        self.forks.append(fork)
        return fork

    def close(self) -> None:
        if self.is_turn:
            self.before_close()
        self.closed += 1


def _assert_busy(store, session_id) -> None:
    with pytest.raises(WorkspaceBusy):
        with workspace_lease(store, session_id, exclusive=True):
            pass


def test_late_raw_fork_binding_error_is_workspace_unavailable(
    tmp_path: Path, terminal
) -> None:
    from tests.yoke.daemon.support import eventually

    root = tmp_path / "workspace"
    root.mkdir()
    provider = ClosingProvider()

    def tools(context: ToolRegistrationContext) -> ToolRegistrationResult:
        return ToolRegistrationResult(tools=[ReadTool.bind(root=context.root)])

    primaries = []

    def factory(_record):
        primary = RuntimeAgent(provider, [], tool_root=root, tool_factory=tools)
        primaries.append(primary)
        return primary

    terminal.factory = factory
    terminal.store.save("cleanup", [], root=root, title="Fixture")
    backend = terminal.backend("cleanup", root=root)
    provider.before_fork = root.rmdir
    provider.before_close = lambda: _assert_busy(terminal.store, "cleanup")
    turn = backend.submit(
        "hello",
        [],
        entries=[],
        stop_event=Event(),
        user_message=None,
        checkpoint=lambda *_: None,
    )
    list(turn)
    outcome = turn.result()
    assert isinstance(outcome, TurnFailure)
    assert (
        backend.connection.session_request("GET")["data"]["workspace"]["status"]
        == "missing"
    )
    assert "--relocate" in str(outcome.error)
    eventually(lambda: len(provider.forks) == 1 and provider.forks[0].closed == 1)
    assert provider.closed == 0
    assert not primaries[0].closed
    assert backend.load_queue().prompts[0].paused
    with workspace_lease(terminal.store, "cleanup", exclusive=True):
        pass


@pytest.mark.parametrize("owned_entries", [False, True])
@pytest.mark.parametrize("delete_root", [False, True])
def test_failed_load_retires_unreturned_fork_without_releasing_lease_early(
    tmp_path: Path, monkeypatch, terminal, owned_entries: bool, delete_root: bool
) -> None:
    from tests.yoke.daemon.support import eventually

    root = tmp_path / "workspace"
    root.mkdir()
    provider = ClosingProvider()
    provider.before_close = lambda: _assert_busy(terminal.store, "cleanup")
    terminal.factory = lambda _record: RuntimeAgent(provider, [], tool_root=root)
    terminal.store.save(
        "cleanup", [Message.user("history")], root=root, title="Fixture"
    )
    backend = terminal.backend("cleanup", root=root)
    runtime = terminal.app.state.runtime_registry.get_if_loaded("cleanup")
    original_prepare = runtime.execution.prepare

    def prepare(record, **kwargs):
        if not owned_entries:
            kwargs["active_entries"] = None
        return original_prepare(record, **kwargs)

    monkeypatch.setattr(runtime.execution, "prepare", prepare)
    waiting, release, finished = Event(), Event(), Event()
    forks = []
    original_close = RuntimeAgent.close

    def fail_load(self, *args, **kwargs):
        forks.append(self)
        if delete_root:
            root.rmdir()
        raise ValueError("state loading failed")

    def close(self):
        if self in forks:
            waiting.set()
            assert release.wait(timeout=5)
        original_close(self)
        if self in forks:
            finished.set()

    monkeypatch.setattr(RuntimeAgent, "load_owned_conversation", fail_load)
    monkeypatch.setattr(RuntimeAgent, "close", close)
    turn = backend.submit(
        "hello",
        [],
        entries=[],
        stop_event=Event(),
        user_message=None,
        checkpoint=lambda *_: None,
    )
    try:
        assert waiting.wait(timeout=3)
        assert len(forks) == 1
        assert not forks[0].closed
        assert provider.forks[0].closed == 0
        _assert_busy(terminal.store, "cleanup")
    finally:
        release.set()
        assert finished.wait(timeout=3)
    list(turn)
    outcome = turn.result()
    assert isinstance(outcome, TurnFailure)
    if delete_root:
        assert (
            backend.connection.session_request("GET")["data"]["workspace"]["status"]
            == "missing"
        )
        assert "--relocate" in str(outcome.error)
    else:
        assert str(outcome.error) == "state loading failed"
    eventually(lambda: provider.forks[0].closed == 1)
    assert forks[0].closed
    assert provider.closed == 0
    backend.connection.session_request("POST", "/drain", params={"timeoutMs": 5000})
    with workspace_lease(terminal.store, "cleanup", exclusive=True):
        pass


def test_rejection_after_preparation_does_not_promote_unexecuted_fork(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    from tests.yoke.daemon.support import eventually
    from .terminal_daemon_support import session_for

    root = tmp_path / "workspace"
    root.mkdir()
    provider = ClosingProvider()
    terminal.factory = lambda _record: RuntimeAgent(provider, [], tool_root=root)
    terminal.store.save("cleanup", [], root=root, title="Named fixture")
    backend = terminal.backend("cleanup", root=root)
    active = session_for(backend)
    runtime = terminal.app.state.runtime_registry.get_if_loaded(active.id)
    assert runtime is not None
    original = runtime.execution.prepare

    def prepare(*args, **kwargs):
        agent = original(*args, **kwargs)
        root.rmdir()
        return agent

    monkeypatch.setattr(runtime.execution, "prepare", prepare)
    promote = Mock(side_effect=AssertionError("promoted unexecuted fork"))
    monkeypatch.setattr(runtime.resources, "promote", promote)
    state = PromptCliState(messages=[], pending_prompts=[], active_turn_id=1)
    lock, outcomes = Lock(), []
    turns.run_prompt_turn(
        turn_id=1,
        prompt="hello",
        state=state,
        state_lock=lock,
        agent=backend,
        active_session=active,
        stop_event=Event(),
        user_message=Message.user("hello"),
        handle_outcome=lambda _id, result: outcomes.append(result),
        turn_renderer_factory=Mock(),
    )
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, TurnFailure)
    assert "--relocate" in str(outcome.error)
    assert outcome.agent is None
    promote.assert_not_called()
    assert (
        turns.handle_prompt_turn_outcome(
            turn_id=1,
            outcome=outcome,
            state=state,
            state_lock=lock,
            agent=backend,
            active_session=active,
            renderer=Mock(),
            scrollback=Mock(),
        )
        is False
    )
    queued = backend.load_queue().prompts
    assert len(queued) == 1
    assert queued[0].prompt == "hello"
    assert queued[0].paused
    assert terminal.store.load(active.id).messages == []
    eventually(lambda: len(provider.forks) == 1 and provider.forks[0].closed == 1)
    assert provider.closed == 0
