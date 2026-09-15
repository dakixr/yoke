"""Live deletion, fresh configuration and resource cleanup at CLI boundaries."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from unittest.mock import Mock

import pytest

from yoke.agent.models import Message
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.skills.models import ActiveSkill
from yoke.cli.bootstrap.config import ToolDiscoveryProvider
from yoke.cli.config import CLIArgs
from yoke.cli.config import runtime as config_runtime
from yoke.cli.interactive.basic import _start_basic_turn
from yoke.cli.interactive.common import BasicCliState, PromptCliState, TurnFailure
from yoke.cli.interactive.common import PendingPrompt, TurnStopped, TurnSuccess
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.interactive.prompt.lifecycle import PersistentPromptLifecycle
from yoke.cli.interactive.prompt.lifecycle import _Submission
from yoke.cli.interactive.prompt.turns import run_prompt_turn
from yoke.cli.interactive.prompt import turns
from yoke.cli.interactive.prompt import runtime as turn_runtime
from yoke.cli.interactive.queue.mutations import append_prompt, dequeue_prompt
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.cli.render import InteractiveRenderer, build_console
from yoke.cli.runtime.cli import run_cli, run_resume_cli
from yoke.cli.runtime.lifetime import close_cli_owned_agent, register_cli_owned_agent
from yoke.cli.runtime.workspaces import retain_workspace_lease
from yoke.cli.runtime.session import create_active_session
from yoke.cli.session import SessionStore
from yoke.session.workspace import WorkspaceBusy, WorkspaceUnavailable, workspace_lease

from .support import CaptureStream, FakeAgent, active_session_for


def test_relocation_rediscovers_configuration_and_skills(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "target"
    skill_dir = target / ".yoke" / "skills" / "fresh-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fresh-skill\ndescription: Newly discovered skill.\n---\nNew instructions.\n",
        encoding="utf-8",
    )
    (target / "AGENTS.md").write_text("TARGET WORKSPACE INSTRUCTIONS", encoding="utf-8")
    (target / ".yoke" / "config.json").write_text(
        '{"tools": {"read": "deny"}}', encoding="utf-8"
    )
    store = SessionStore()
    historical_skill = ActiveSkill(
        name="old-skill",
        description="Historical skill",
        source_path=str(tmp_path / "missing" / "SKILL.md"),
        content="Cached old instructions",
    )
    original = store.save(
        "saved",
        [Message.user("old history")],
        root=tmp_path / "missing",
        active_skills=[historical_skill],
        skill_dirs=[str(tmp_path / "old-skills")],
    )
    monkeypatch.setattr(
        config_runtime,
        "_build_startup_provider",
        lambda args, **kwargs: (ToolDiscoveryProvider(), None),
    )
    original_resolve = config_runtime._resolve_cli_agent_config
    resolutions = []

    def resolve(**kwargs):
        resolved = original_resolve(**kwargs)
        resolutions.append(resolved)
        assert kwargs["root"] == target
        return resolved

    monkeypatch.setattr(config_runtime, "_resolve_cli_agent_config", resolve)

    def interactive(_args, agent, messages, **kwargs):
        assert any(skill.name == "fresh-skill" for skill in agent.available_skills)
        assert agent.active_skills == [historical_skill]
        assert agent.tool_report.config_path == target / ".yoke" / "config.json"
        assert "read" not in agent.tools
        assert (
            kwargs["active_session"].record.conversation_entries
            == original.conversation_entries
        )
        assert messages == original.messages
        assert any(
            "TARGET WORKSPACE INSTRUCTIONS" in message.text_content()
            for message in resolutions[0].system_messages
        )
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)),
            "saved",
            relocate=target,
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 0
    )
    assert store.load("saved").active_skills == [historical_skill]


@pytest.mark.parametrize("fail", [False, True])
def test_owned_agent_and_provider_close_before_lease_release(
    tmp_path: Path, fail: bool
) -> None:
    store = SessionStore()
    store.save("saved", [], root=tmp_path)
    cleanup = []

    def close_resource(name: str) -> None:
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(store, "saved", exclusive=True):
                pass
        cleanup.append(name)

    agent = Mock()
    agent.close.side_effect = lambda: close_resource("agent")
    agent.provider.close.side_effect = lambda: close_resource("provider")

    @close_cli_owned_agent
    def invocation() -> None:
        retain_workspace_lease(store, "saved")
        register_cli_owned_agent(agent)
        if fail:
            raise ValueError("failed during persistence")

    if fail:
        with pytest.raises(ValueError, match="failed during persistence"):
            invocation()
    else:
        invocation()
    assert cleanup == ["agent", "provider"]
    with workspace_lease(store, "saved", exclusive=True):
        pass


def test_headless_rechecks_workspace_after_agent_construction(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    agent = FakeAgent()

    def resolve(*args, **kwargs):
        root.rmdir()
        return agent, None

    monkeypatch.setattr("yoke.cli.runtime.cli._resolve_runtime_agent", resolve)
    assert (
        run_cli(
            CLIArgs(root=str(root), prompt="hello", headless=True),
            agent=agent,
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 1
    )
    assert not agent.seen_history_lengths
    assert SessionStore().list() == []


@pytest.mark.parametrize("mode", ["basic", "prompt"])
def test_deleted_workspace_reports_turn_failure_without_executing(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    root.rmdir()
    agent = FakeAgent()
    prepare = Mock(side_effect=AssertionError("forked provider before validation"))
    monkeypatch.setattr("yoke.cli.interactive.prompt.turns.prepare_turn_agent", prepare)
    results: Queue[TurnSuccess | TurnStopped | TurnFailure] = Queue()
    if mode == "basic":
        thread = _start_basic_turn(
            "hello",
            state=BasicCliState(messages=[], pending_prompts=[]),
            active_session=active,
            agent=agent,
            stderr=CaptureStream(),
            renderer=InteractiveRenderer(CaptureStream()),
            result_queue=results,
        )
        thread.join(timeout=2)
        assert not thread.is_alive()
    else:
        run_prompt_turn(
            turn_id=1,
            prompt="hello",
            state=PromptCliState(messages=[], pending_prompts=[]),
            state_lock=Lock(),
            agent=agent,
            active_session=active,
            stop_event=Event(),
            user_message=None,
            callbacks={"handle_outcome": lambda turn_id, outcome: results.put(outcome)},
            turn_renderer_factory=Mock(),
        )
    outcome = results.get(timeout=1)
    assert isinstance(outcome, TurnFailure)
    assert isinstance(outcome.error, WorkspaceUnavailable)
    assert "--relocate" in str(outcome.error)
    assert not agent.seen_history_lengths
    prepare.assert_not_called()


def test_missing_workspace_keeps_queue_and_restores_submitted_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    state = PromptCliState(messages=[], pending_prompts=[])
    lock = Lock()
    queued = PendingPrompt("queued work")
    assert append_prompt(
        state=state, state_lock=lock, active_session=active, prompt=queued
    )
    before = load_prompt_queue_state(active)
    root.rmdir()
    assert dequeue_prompt(state=state, state_lock=lock, active_session=active) is None
    assert state.pending_prompts == [queued]
    assert "--relocate" in (state.status_message or "")
    assert load_prompt_queue_state(active) == before
    start = Mock()
    with pytest.raises(WorkspaceUnavailable):
        submit_prompt_toolkit_prompt(
            "new work",
            action="queue",
            state=state,
            active_session=active,
            state_lock=lock,
            invalidate_prompt=lambda: None,
            start_turn=start,
            steer_active_turn=Mock(),
        )
    assert state.next_editor_text == "new work"
    start.assert_not_called()
    assert load_prompt_queue_state(active) == before
    root.mkdir()
    assert dequeue_prompt(state=state, state_lock=lock, active_session=active) == queued


@pytest.mark.parametrize("command", ["/fork", "/new"])
def test_deleted_workspace_session_switch_is_recoverable(
    tmp_path: Path, command: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    active = active_session_for(root)
    root.rmdir()
    stream = CaptureStream()
    handled, messages, updated = handle_slash_command(
        command,
        agent=FakeAgent(),
        active_session=active,
        messages=[],
        console=build_console(stream),
    )
    assert handled
    assert messages == []
    assert updated is active
    assert "--relocate" in stream.getvalue()
    assert SessionStore().list() == []


@pytest.mark.parametrize("prompt", ["/new", "ordinary prompt"])
def test_prompt_submission_executor_retains_new_session_lease(
    tmp_path: Path, prompt: str
) -> None:
    store = SessionStore()
    seen = []

    def submit(_prompt: str, _action: str) -> None:
        active = create_active_session(CLIArgs(root=str(tmp_path)), root=tmp_path)
        seen.append(active.id)
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(store, active.id, exclusive=True):
                pass

    config = Mock()
    config.process_submission = submit
    lifecycle = PersistentPromptLifecycle(config)

    @close_cli_owned_agent
    def invocation() -> None:
        try:
            asyncio.run(lifecycle._process_submission(_Submission(prompt, "steer")))
            with pytest.raises(WorkspaceBusy):
                with workspace_lease(store, seen[0], exclusive=True):
                    pass
        finally:
            lifecycle._submission_executor.shutdown(wait=True)

    invocation()
    with workspace_lease(store, seen[0], exclusive=True):
        pass


def test_retired_turn_keeps_lease_until_detached_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    active = active_session_for(tmp_path)
    primary = RuntimeAgent(ToolDiscoveryProvider(), tools=[], tool_root=tmp_path)
    release, waiting, finished = Event(), Event(), Event()
    real_take = turn_runtime.take_turn_workspace

    def wait(_tools) -> None:
        waiting.set()
        assert release.wait(timeout=2)

    @contextmanager
    def take(agent):
        with real_take(agent):
            yield
        finished.set()

    monkeypatch.setattr(turn_runtime, "wait_for_in_process_tools", wait)
    monkeypatch.setattr(turn_runtime, "take_turn_workspace", take)

    @close_cli_owned_agent
    def invocation() -> None:
        retain_workspace_lease(active.store, active.id)
        register_cli_owned_agent(primary)
        fork = turns.prepare_turn_agent(
            primary, messages=[], entries=[], active_session=active
        )
        turns.retire_turn_agent(fork, primary_agent=primary)

    try:
        invocation()
        assert waiting.wait(timeout=1)
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(active.store, active.id, exclusive=True):
                pass
    finally:
        release.set()
        assert finished.wait(timeout=2)
    with workspace_lease(active.store, active.id, exclusive=True):
        pass
