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
from yoke.cli.interactive.prompt import runtime
from yoke.cli.interactive.prompt import turns
from yoke.cli.interactive.common import PromptCliState, TurnFailure
from yoke.session.workspace import WorkspaceBusy, WorkspaceUnavailable, workspace_lease

from .support import active_session_for


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
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    provider = ClosingProvider()
    primary = RuntimeAgent(provider, [], tool_root=root)
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        active_turn_id=1,
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    lock, outcomes, retired = Lock(), [], []
    monkeypatch.setattr(
        turns, "start_session_title_generation", lambda *a, **k: root.rmdir()
    )
    monkeypatch.setattr(
        turns,
        "promote_runtime_fork",
        Mock(side_effect=AssertionError("promoted unexecuted fork")),
    )
    monkeypatch.setattr(
        turns, "retire_turn_agent", lambda agent, **kwargs: retired.append(agent)
    )
    try:
        turns.run_prompt_turn(
            turn_id=1,
            prompt="hello",
            state=state,
            state_lock=lock,
            agent=primary,
            active_session=active,
            stop_event=Event(),
            user_message=Message.user("hello"),
            callbacks={"handle_outcome": lambda _id, result: outcomes.append(result)},
            turn_renderer_factory=Mock(),
        )
        outcome = outcomes[0]
        assert isinstance(outcome, TurnFailure)
        assert isinstance(outcome.error, WorkspaceUnavailable)
        assert outcome.agent is not primary
        assert outcome.rejected_prompt is not None
        assert (
            turns.handle_prompt_turn_outcome(
                turn_id=1,
                outcome=outcome,
                state=state,
                state_lock=lock,
                agent=primary,
                active_session=active,
                renderer=Mock(),
                scrollback=Mock(),
            )
            is False
        )
        assert retired == [outcome.agent]
        assert state.pending_prompts[0].prompt == "hello"
        assert state.pending_prompts[0].paused
    finally:
        for agent in retired:
            with runtime.take_turn_workspace(agent):
                agent.close()
                agent.provider.close()
        primary.close()
