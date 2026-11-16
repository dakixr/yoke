"""Unreturned CLI forks retain leases until their resources are closed."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from unittest.mock import Mock

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.tools import ReadTool, ToolRegistrationContext, ToolRegistrationResult
from yoke.cli.interactive.common import PromptCliState, TurnFailure
from yoke.cli.interactive.prompt import turns
from yoke.cli.runtime import turn_agent as runtime
from yoke.session.workspace import WorkspaceBusy, WorkspaceUnavailable, workspace_lease

from .support import active_session_for
from .terminal_daemon_support import terminal as terminal


class ClosingProvider:
    def __init__(self) -> None:
        self.closed = 0
        self.forks: list[ClosingProvider] = []
        self.before_fork = lambda: None
        self.before_close = lambda: None

    def complete(self, messages, tools) -> Message:
        raise AssertionError("No model calls are allowed")

    def fork_for_turn(self) -> ClosingProvider:
        self.before_fork()
        fork = ClosingProvider()
        fork.before_close = self.before_close
        self.forks.append(fork)
        return fork

    def close(self) -> None:
        self.before_close()
        self.closed += 1


def _assert_busy(active) -> None:
    with pytest.raises(WorkspaceBusy):
        with workspace_lease(active.store, active.id, exclusive=True):
            pass


def test_late_raw_fork_binding_error_is_workspace_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    provider = ClosingProvider()

    def tools(context: ToolRegistrationContext) -> ToolRegistrationResult:
        return ToolRegistrationResult(tools=[ReadTool.bind(root=context.root)])

    primary = RuntimeAgent(provider, [], tool_root=root, tool_factory=tools)
    provider.before_fork = root.rmdir
    provider.before_close = lambda: _assert_busy(active)
    try:
        with pytest.raises(WorkspaceUnavailable) as error:
            runtime.prepare_turn_agent(
                primary, messages=[], entries=[], active_session=active
            )
        assert error.value.details["status"] == "missing"
        assert "--relocate" in str(error.value)
        assert provider.forks[0].closed == 1
        assert provider.closed == 0
        assert not primary.closed
        with workspace_lease(active.store, active.id, exclusive=True):
            pass
    finally:
        primary.close()


@pytest.mark.parametrize("owned_entries", [False, True])
@pytest.mark.parametrize("delete_root", [False, True])
def test_failed_load_retires_unreturned_fork_without_releasing_lease_early(
    tmp_path: Path, monkeypatch, owned_entries: bool, delete_root: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    provider = ClosingProvider()
    provider.before_close = lambda: _assert_busy(active)
    primary = RuntimeAgent(provider, [], tool_root=root)
    waiting, release, finished = Event(), Event(), Event()
    forks = []
    real_take = runtime.take_turn_workspace

    def fail_load(self, *args, **kwargs):
        forks.append(self)
        if delete_root:
            root.rmdir()
        raise ValueError("state loading failed")

    def wait(_tools):
        waiting.set()
        assert release.wait(timeout=3)

    @contextmanager
    def take(agent):
        with real_take(agent):
            yield
        finished.set()

    monkeypatch.setattr(
        RuntimeAgent,
        "load_owned_conversation" if owned_entries else "load_conversation",
        fail_load,
    )
    monkeypatch.setattr(runtime, "wait_for_in_process_tools", wait)
    monkeypatch.setattr(runtime, "take_turn_workspace", take)
    entries = (
        conversation_entries_from_messages([Message.user("history")])
        if owned_entries
        else []
    )
    try:
        with pytest.raises(ValueError) as error:
            runtime.prepare_turn_agent(
                primary, messages=[], entries=entries, active_session=active
            )
        if delete_root:
            assert isinstance(error.value, WorkspaceUnavailable)
            assert error.value.details["status"] == "missing"
        else:
            assert type(error.value) is ValueError
            assert str(error.value) == "state loading failed"
        assert waiting.wait(timeout=1)
        assert len(forks) == 1
        assert not forks[0].closed
        assert provider.forks[0].closed == 0
        _assert_busy(active)
    finally:
        release.set()
        assert finished.wait(timeout=3)
        primary.close()
    assert forks[0].closed
    assert provider.forks[0].closed == 1
    assert provider.closed == 0
    with workspace_lease(active.store, active.id, exclusive=True):
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
