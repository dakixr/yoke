"""Retain local lifetime invariants until the compatibility executor is deleted."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.runtime import workspaces
from yoke.cli.runtime.cli import run_cli, run_resume_cli
from yoke.cli.runtime.lifetime import close_cli_owned_agent, register_cli_owned_agent
from yoke.cli.runtime.session import create_active_session, fork_active_session
from yoke.session import SessionStore
from yoke.session.workspace import (
    WorkspaceBusy,
    relocate_session_workspace,
    workspace_lease,
)

from .support import FakeAgent


def test_close_cli_owned_agent_preserves_entrypoint_error() -> None:
    class FailingCloseAgent:
        closed = False

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    agent = FailingCloseAgent()

    @close_cli_owned_agent
    def entrypoint() -> None:
        register_cli_owned_agent(agent)
        raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        entrypoint()
    assert agent.closed is True


def test_close_cli_owned_agent_tolerates_close_failure() -> None:
    class FailingCloseAgent:
        closed = False

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    agent = FailingCloseAgent()

    @close_cli_owned_agent
    def entrypoint() -> str:
        register_cli_owned_agent(agent)
        return "ok"

    assert entrypoint() == "ok"
    assert agent.closed is True


def test_close_cli_owned_agent_leaves_caller_owned_agent() -> None:
    class CloseTrackingAgent:
        closed = False

        def close(self) -> None:
            self.closed = True

    agent = CloseTrackingAgent()

    @close_cli_owned_agent
    def entrypoint(agent: object | None = None) -> str:
        return "ok"

    assert entrypoint(agent=agent) == "ok"
    assert agent.closed is False


@pytest.mark.parametrize("mode", ["resume", "session"])
def test_use_lease_precedes_load_and_survives_switches_and_cleanup(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    store = SessionStore()
    store.save("saved", [Message.user("old")], root=tmp_path)
    real_load = SessionStore.load
    seen_ids = ["saved"]

    def load(self, session_id):
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(self, session_id, exclusive=True):
                pass
        return real_load(self, session_id)

    monkeypatch.setattr(SessionStore, "load", load)

    def interactive(_args, agent, messages, **kwargs):
        active = kwargs["active_session"]
        fork = fork_active_session(active, agent, messages)
        new = create_active_session(CLIArgs(root=str(tmp_path)), root=tmp_path)
        seen_ids.extend([fork.id, new.id])
        for session_id in seen_ids:
            with pytest.raises(WorkspaceBusy):
                with workspace_lease(store, session_id, exclusive=True):
                    pass
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    if mode == "resume":
        assert (
            run_resume_cli(CLIArgs(root=str(tmp_path)), "saved", agent=FakeAgent()) == 0
        )
    else:
        assert (
            run_cli(CLIArgs(root=str(tmp_path), session="saved"), agent=FakeAgent())
            == 0
        )
    for session_id in seen_ids:
        with workspace_lease(store, session_id, exclusive=True):
            pass


def test_reload_after_relocation_observes_binding_under_use_lease(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore()
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store.save("saved", [], root=tmp_path)
    real_lease = workspaces.workspace_lease

    @contextmanager
    def raced_lease(store, session_id):
        relocate_session_workspace(store, session_id, second)
        with real_lease(store, session_id):
            yield

    monkeypatch.setattr(workspaces, "workspace_lease", raced_lease)

    def interactive(args, _agent, _messages, **kwargs):
        assert args.root == str(second)
        assert kwargs["active_session"].root == second
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)), "saved", relocate=first, agent=FakeAgent()
        )
        == 0
    )
