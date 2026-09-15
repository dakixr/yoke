"""Workspace recovery must not construct providers or rewrite saved history."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from yoke.agent.models import Message
from yoke.cli.main import app
from yoke.cli.runtime.cli import run_cli, run_resume_cli
from yoke.cli.config import CLIArgs
from yoke.cli.runtime import workspaces
from yoke.cli.runtime.session import create_active_session, fork_active_session
from yoke.cli.session import SessionStore
from yoke.session.workspace import (
    WorkspaceBusy,
    WorkspaceUnavailable,
    relocate_session_workspace,
)
from yoke.session.workspace import workspace_lease

from .support import CaptureStream, FakeAgent


@pytest.mark.parametrize("mode", ["new", "session", "resume", "fork"])
@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("invalid", ["missing", "file"])
def test_invalid_workspace_rejected_before_agent_construction(
    tmp_path: Path, monkeypatch, mode: str, injected: bool, invalid: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    store = SessionStore()
    store.save("saved", [Message.user("history")], root=root)
    before = (store.directory / "saved.jsonl").read_bytes()
    root.rmdir()
    if invalid == "file":
        root.write_text("not a directory", encoding="utf-8")
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.runtime.startup.build_cli_agent_from_args", builder)
    agent = FakeAgent() if injected else None
    args = CLIArgs(root=str(root), headless=True, prompt="hello")
    stderr = CaptureStream()
    if mode == "resume":
        # A valid selector root is never a fallback for a saved missing root.
        args.root = str(tmp_path)
        code = run_resume_cli(
            args, "saved", agent=agent, stdout=CaptureStream(), stderr=stderr
        )
    else:
        if mode == "session":
            args.session = "saved"
            args.root = str(tmp_path)
        elif mode == "fork":
            args.fork_session_id = "saved"
        code = run_cli(args, agent=agent, stdout=CaptureStream(), stderr=stderr)
    assert code == 1
    builder.assert_not_called()
    if agent is not None:
        assert agent.seen_history_lengths == []
    assert (store.directory / "saved.jsonl").read_bytes() == before
    assert [record.id for record in store.list()] == ["saved"]
    assert "Falling back" not in stderr.getvalue()
    if mode in {"resume", "session"}:
        assert "yoke resume saved --relocate" in " ".join(stderr.getvalue().split())
    if invalid == "missing":
        assert not root.exists()


def test_relocation_requires_explicit_id_before_selecting_or_building(
    tmp_path: Path, monkeypatch
) -> None:
    select = Mock(side_effect=AssertionError("opened chooser"))
    monkeypatch.setattr("yoke.cli.runtime.cli.select_session_id", select)
    stderr = CaptureStream()
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)),
            None,
            relocate=tmp_path,
            agent=FakeAgent(),
            stdout=CaptureStream(),
            stderr=stderr,
        )
        == 1
    )
    assert "requires an explicit session ID" in stderr.getvalue()
    select.assert_not_called()


def test_resume_command_passes_explicit_relocation(tmp_path: Path, monkeypatch) -> None:
    run = Mock(return_value=0)
    monkeypatch.setattr("yoke.cli.runtime.run_resume_cli", run)
    result = CliRunner().invoke(app, ["resume", "saved", "--relocate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert run.call_args.args[1] == "saved"
    assert run.call_args.kwargs["relocate"] == tmp_path


def test_relocation_preserves_history_and_noop_does_not_write(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore()
    old = tmp_path / "deleted"
    old.mkdir()
    original = store.save("saved", [Message.user("historical prompt")], root=old)
    old.rmdir()
    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", lambda *a, **k: 0)
    args = CLIArgs(root=str(tmp_path / "irrelevant-selector"))
    assert (
        run_resume_cli(
            args, "saved", relocate=tmp_path, agent=FakeAgent(), stdout=CaptureStream()
        )
        == 0
    )
    relocated = store.load("saved")
    assert relocated.id == original.id
    assert relocated.conversation_entries == original.conversation_entries
    assert relocated.leaf_id == original.leaf_id
    assert relocated.root == str(tmp_path)
    assert not old.exists()
    before = (store.directory / "saved.jsonl").read_bytes()
    assert (
        run_resume_cli(
            args, "saved", relocate=tmp_path, agent=FakeAgent(), stdout=CaptureStream()
        )
        == 0
    )
    assert (store.directory / "saved.jsonl").read_bytes() == before


@pytest.mark.parametrize("target_kind", ["missing", "file"])
def test_invalid_relocation_leaves_saved_session_unchanged(
    tmp_path: Path, monkeypatch, target_kind: str
) -> None:
    store = SessionStore()
    store.save("saved", [Message.user("old")], root=tmp_path)
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("file", encoding="utf-8")
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.runtime.startup.build_cli_agent_from_args", builder)
    before = (store.directory / "saved.jsonl").read_bytes()
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)),
            "saved",
            relocate=target,
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 1
    )
    builder.assert_not_called()
    assert (store.directory / "saved.jsonl").read_bytes() == before


def test_root_selector_does_not_relocate_saved_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore()
    root = tmp_path / "original"
    root.mkdir()
    store.save("saved", [], root=root)

    def interactive(args, _agent, _messages, **kwargs):
        assert args.root == str(root)
        assert kwargs["active_session"].root == root
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)),
            "saved",
            agent=FakeAgent(),
            stdout=CaptureStream(),
        )
        == 0
    )
    assert store.load("saved").root == str(root)


@pytest.mark.parametrize("mode", ["resume", "session"])
@pytest.mark.parametrize("history", [False, True])
def test_rootless_saved_records_require_explicit_relocation(
    tmp_path: Path, monkeypatch, mode: str, history: bool
) -> None:
    store = SessionStore()
    original = store.save("legacy", [Message.user("old")] if history else [])
    assert store.load("legacy").root is None
    with pytest.raises(WorkspaceUnavailable) as error:
        workspaces.session_workspace(original)
    assert error.value.details["status"] == "unconfigured"
    before = (store.directory / "legacy.jsonl").read_bytes()
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.runtime.startup.build_cli_agent_from_args", builder)
    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", lambda *a, **k: 0)
    args = CLIArgs(root=str(tmp_path))
    stderr = CaptureStream()
    if mode == "resume":
        assert run_resume_cli(args, "legacy", stderr=stderr) == 1
    else:
        args.session = "legacy"
        assert run_cli(args, stderr=stderr) == 1
    builder.assert_not_called()
    assert "--relocate" in stderr.getvalue()
    assert (store.directory / "legacy.jsonl").read_bytes() == before
    assert run_resume_cli(args, "legacy", relocate=tmp_path, agent=FakeAgent()) == 0
    relocated = store.load("legacy")
    assert relocated.root == str(tmp_path)
    assert relocated.conversation_entries == original.conversation_entries


@pytest.mark.parametrize("session_id", [None, "fresh"])
def test_only_new_sessions_inherit_invocation_root(tmp_path: Path, session_id) -> None:
    active = create_active_session(
        CLIArgs(root=str(tmp_path), session=session_id), root=tmp_path
    )
    assert active.root == tmp_path


def test_fork_recovers_into_valid_target_without_modifying_source(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore()
    old = tmp_path / "missing"
    original = store.save("source", [Message.user("history")], root=old)
    before = (store.directory / "source.jsonl").read_bytes()
    seen = []

    def interactive(args, _agent, _messages, **kwargs):
        fork = kwargs["active_session"]
        seen.append(fork)
        assert args.root == str(tmp_path)
        assert fork.root == tmp_path
        assert fork.id != "source"
        assert fork.record.conversation_entries == original.conversation_entries
        with pytest.raises(WorkspaceBusy):
            relocate_session_workspace(store, fork.id, tmp_path)
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_cli(
            CLIArgs(root=str(tmp_path), fork_session_id="source"), agent=FakeAgent()
        )
        == 0
    )
    assert len(seen) == 1
    assert (store.directory / "source.jsonl").read_bytes() == before
    assert not old.exists()
    with workspace_lease(store, seen[0].id, exclusive=True):
        pass


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


def test_construction_error_releases_use_lease(tmp_path: Path, monkeypatch) -> None:
    store = SessionStore()
    store.save("saved", [], root=tmp_path)
    monkeypatch.setattr(
        "yoke.cli.runtime.startup.build_cli_agent_from_args",
        Mock(side_effect=ValueError("broken configuration")),
    )
    assert run_resume_cli(CLIArgs(root=str(tmp_path)), "saved") == 1
    with workspace_lease(store, "saved", exclusive=True):
        pass


def test_busy_relocation_is_recoverable_before_provider_construction(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore()
    target = tmp_path / "target"
    target.mkdir()
    store.save("saved", [Message.user("history")], root=tmp_path)
    before = (store.directory / "saved.jsonl").read_bytes()
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.runtime.startup.build_cli_agent_from_args", builder)
    stderr = CaptureStream()
    with workspace_lease(store, "saved"):
        result = run_resume_cli(
            CLIArgs(root=str(tmp_path)), "saved", relocate=target, stderr=stderr
        )
    assert result == 1
    assert "in use" in stderr.getvalue()
    assert (store.directory / "saved.jsonl").read_bytes() == before
    builder.assert_not_called()


@pytest.mark.parametrize("root", ["", "bad\x00path"])
def test_invalid_fresh_path_is_not_a_cwd_fallback(root: str) -> None:
    agent = FakeAgent()
    assert (
        run_cli(
            CLIArgs(root=root, headless=True, prompt="hello"),
            agent=agent,
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 1
    )
    assert agent.seen_history_lengths == []
    assert SessionStore().list() == []
