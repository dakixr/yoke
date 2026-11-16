"""Workspace recovery must not construct providers or rewrite saved history."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from yoke.agent.models import Message
from yoke.cli.main import app
from yoke.cli.backend.startup import (
    run_daemon_cli as run_cli,
    run_daemon_headless,
    run_daemon_interactive,
)
from yoke.cli.config import CLIArgs
from yoke.session import SessionStore
from yoke.session.workspace import (
    WorkspaceUnavailable,
    require_session_workspace,
)
from yoke.session.workspace import workspace_lease

from .support import CaptureStream
from .terminal_daemon_support import ReplyProvider, terminal_daemon
from yoke.cli.config import runtime as config_runtime
from tests.yoke.session_rows import stored_session_bytes


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    del tmp_path
    with terminal_daemon(monkeypatch) as daemon:
        monkeypatch.setattr(
            "yoke.http.services.session_startup.build_cli_agent_from_args",
            config_runtime.build_cli_agent_from_args,
        )
        monkeypatch.setattr(
            config_runtime,
            "_build_startup_provider",
            lambda *_args, **_kwargs: (ReplyProvider(), None),
        )
        yield daemon


def run_resume_cli(args, session_id, *, relocate=None, **kwargs):
    run = run_daemon_headless if args.headless else run_daemon_interactive
    return run(args, resume_id=session_id, relocate=relocate, **kwargs)


@pytest.mark.parametrize("mode", ["new", "session", "resume", "fork"])
@pytest.mark.parametrize("invalid", ["missing", "file"])
def test_invalid_workspace_rejected_before_agent_construction(
    tmp_path: Path, monkeypatch, mode: str, invalid: str, terminal
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    store = SessionStore()
    store.save("saved", [Message.user("history")], root=root)
    before = stored_session_bytes(store, "saved")
    root.rmdir()
    if invalid == "file":
        root.write_text("not a directory", encoding="utf-8")
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.config.runtime._build_startup_provider", builder)
    args = CLIArgs(root=str(root), headless=True, prompt="hello")
    stderr = CaptureStream()
    if mode == "resume":
        # A valid selector root is never a fallback for a saved missing root.
        args.root = str(tmp_path)
        code = run_resume_cli(args, "saved", stdout=CaptureStream(), stderr=stderr)
    else:
        if mode == "session":
            args.session = "saved"
            args.root = str(tmp_path)
        elif mode == "fork":
            args.fork_session_id = "saved"
        code = run_cli(args, stdout=CaptureStream(), stderr=stderr)
    assert code == 1
    builder.assert_not_called()
    assert stored_session_bytes(store, "saved") == before
    assert [record.id for record in store.list()] == ["saved"]
    assert "Falling back" not in stderr.getvalue()
    if mode in {"resume", "session"}:
        assert "yoke resume saved --relocate" in " ".join(stderr.getvalue().split())
    if invalid == "missing":
        assert not root.exists()


def test_relocation_requires_explicit_id_before_selecting_or_building(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    select = Mock(side_effect=AssertionError("opened chooser"))
    monkeypatch.setattr("builtins.input", select)
    stderr = CaptureStream()
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)),
            None,
            relocate=tmp_path,
            stdout=CaptureStream(),
            stderr=stderr,
        )
        == 1
    )
    assert "requires an explicit session ID" in stderr.getvalue()
    select.assert_not_called()


def test_resume_command_passes_explicit_relocation(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    run = Mock(return_value=0)
    monkeypatch.setattr("yoke.cli.backend.startup.run_daemon_interactive", run)
    result = CliRunner().invoke(app, ["resume", "saved", "--relocate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["resume_id"] == "saved"
    assert run.call_args.kwargs["relocate"] == tmp_path


def test_relocation_preserves_history_and_noop_does_not_write(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    store = SessionStore()
    old = tmp_path / "deleted"
    old.mkdir()
    original = store.save("saved", [Message.user("historical prompt")], root=old)
    old.rmdir()
    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", lambda *a, **k: 0)
    args = CLIArgs(root=str(tmp_path / "irrelevant-selector"))
    assert run_resume_cli(args, "saved", relocate=tmp_path, stdout=CaptureStream()) == 0
    relocated = store.load("saved")
    assert relocated.id == original.id
    assert relocated.conversation_entries == original.conversation_entries
    assert relocated.leaf_id == original.leaf_id
    assert relocated.root == str(tmp_path)
    assert not old.exists()
    before = stored_session_bytes(store, "saved")
    assert run_resume_cli(args, "saved", relocate=tmp_path, stdout=CaptureStream()) == 0
    assert stored_session_bytes(store, "saved") == before


@pytest.mark.parametrize("target_kind", ["missing", "file"])
def test_invalid_relocation_leaves_saved_session_unchanged(
    tmp_path: Path, monkeypatch, target_kind: str, terminal
) -> None:
    store = SessionStore()
    store.save("saved", [Message.user("old")], root=tmp_path)
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("file", encoding="utf-8")
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.config.runtime._build_startup_provider", builder)
    before = stored_session_bytes(store, "saved")
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
    assert stored_session_bytes(store, "saved") == before


def test_root_selector_does_not_relocate_saved_workspace(
    tmp_path: Path, monkeypatch, terminal
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
            stdout=CaptureStream(),
        )
        == 0
    )
    assert store.load("saved").root == str(root)


@pytest.mark.parametrize("mode", ["resume", "session"])
@pytest.mark.parametrize("history", [False, True])
def test_rootless_saved_records_require_explicit_relocation(
    tmp_path: Path, monkeypatch, mode: str, history: bool, terminal
) -> None:
    store = SessionStore()
    original = store.save("legacy", [Message.user("old")] if history else [])
    assert store.load("legacy").root is None
    with pytest.raises(WorkspaceUnavailable) as error:
        require_session_workspace(original)
    assert error.value.details["status"] == "unconfigured"
    before = stored_session_bytes(store, "legacy")
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.config.runtime._build_startup_provider", builder)
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
    assert stored_session_bytes(store, "legacy") == before
    monkeypatch.setattr(
        config_runtime,
        "_build_startup_provider",
        lambda *_args, **_kwargs: (ReplyProvider(), None),
    )
    assert run_resume_cli(args, "legacy", relocate=tmp_path) == 0
    relocated = store.load("legacy")
    assert relocated.root == str(tmp_path)
    assert relocated.conversation_entries == original.conversation_entries


@pytest.mark.parametrize("session_id", [None, "fresh"])
def test_only_new_sessions_inherit_invocation_root(
    tmp_path: Path, session_id, terminal
) -> None:
    active = terminal.backend(session_id or "generated", root=tmp_path).session
    assert active.root == tmp_path


def test_fork_recovers_into_valid_target_without_modifying_source(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    store = SessionStore()
    old = tmp_path / "missing"
    original = store.save("source", [Message.user("history")], root=old)
    before = stored_session_bytes(store, "source")
    seen = []

    def interactive(args, _agent, _messages, **kwargs):
        fork = kwargs["active_session"]
        seen.append(fork)
        assert args.root == str(tmp_path)
        assert fork.root == tmp_path
        assert fork.id != "source"
        assert store.load(fork.id).conversation_entries == original.conversation_entries
        assert _agent.connection.session_request(
            "POST", "/relocate", json={"directory": str(tmp_path)}
        )["data"]["location"]["directory"] == str(tmp_path)
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert run_cli(CLIArgs(root=str(tmp_path), fork_session_id="source")) == 0
    assert len(seen) == 1
    assert stored_session_bytes(store, "source") == before
    assert not old.exists()
    with workspace_lease(store, seen[0].id, exclusive=True):
        pass


def test_construction_error_releases_use_lease(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    store = SessionStore()
    store.save("saved", [], root=tmp_path)
    monkeypatch.setattr(
        "yoke.cli.config.runtime._build_startup_provider",
        Mock(side_effect=ValueError("broken configuration")),
    )
    assert run_resume_cli(CLIArgs(root=str(tmp_path)), "saved") == 1
    with workspace_lease(store, "saved", exclusive=True):
        pass


def test_busy_relocation_is_recoverable_before_provider_construction(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    store = SessionStore()
    target = tmp_path / "target"
    target.mkdir()
    store.save("saved", [Message.user("history")], root=tmp_path)
    before = stored_session_bytes(store, "saved")
    builder = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch.setattr("yoke.cli.config.runtime._build_startup_provider", builder)
    stderr = CaptureStream()
    with workspace_lease(store, "saved"):
        result = run_resume_cli(
            CLIArgs(root=str(tmp_path)), "saved", relocate=target, stderr=stderr
        )
    assert result == 1
    assert "in use" in stderr.getvalue()
    assert stored_session_bytes(store, "saved") == before
    builder.assert_not_called()


@pytest.mark.parametrize("root", ["", "bad\x00path"])
def test_invalid_fresh_path_is_not_a_cwd_fallback(root: str, terminal) -> None:
    provider = Mock(side_effect=AssertionError("constructed provider"))
    monkeypatch = pytest.MonkeyPatch()
    terminal.stack.callback(monkeypatch.undo)
    monkeypatch.setattr("yoke.cli.config.runtime._build_startup_provider", provider)
    assert (
        run_cli(
            CLIArgs(root=root, headless=True, prompt="hello"),
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 1
    )
    provider.assert_not_called()
    assert SessionStore().list() == []
