"""Live deletion, fresh configuration and resource cleanup at CLI boundaries."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations


import asyncio
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from unittest.mock import Mock

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message, MessageTextContentPart
from yoke.agent.skills.models import ActiveSkill
from yoke.cli.bootstrap.config import ToolDiscoveryProvider
from yoke.cli.config import CLIArgs
from yoke.cli.config import runtime as config_runtime
from yoke.cli.interactive.basic import _start_basic_turn
from yoke.cli.interactive.commands import handle_slash_command
from yoke.cli.interactive.common import (
    BasicCliState,
    PendingPrompt,
    PromptCliState,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.prompt.lifecycle import PersistentPromptLifecycle, _Submission
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.interactive.prompt.turns import run_prompt_turn
from yoke.cli.interactive.queue.mutations import queue_prompt_submission, dequeue_prompt
from .daemon_queue_support import load_prompt_queue_state
from yoke.cli.render import InteractiveRenderer, build_console
from yoke.cli.backend.startup import run_daemon_cli, run_daemon_interactive
from yoke.session import SessionStore
from yoke.session.workspace import WorkspaceBusy, WorkspaceUnavailable, workspace_lease

from .support import CaptureStream
from .terminal_daemon_support import ReplyProvider, session_for, terminal as terminal
from tests.yoke.daemon.support import eventually


def test_relocation_rediscovers_configuration_and_skills(
    tmp_path: Path, monkeypatch, terminal
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
    monkeypatch.setattr(
        "yoke.http.services.session_startup.build_cli_agent_from_args",
        config_runtime.build_cli_agent_from_args,
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
        assert any(skill.name == "fresh-skill" for skill in agent.available_skills())
        assert store.load("saved").active_skills == [historical_skill]
        assert (
            resolutions[0].tool_report.config_path == target / ".yoke" / "config.json"
        )
        assert "read" not in agent.tools().active_names()
        assert (
            store.load(kwargs["active_session"].id).conversation_entries
            == original.conversation_entries
        )
        assert messages == [
            message.model_copy(
                update={
                    "content": [
                        MessageTextContentPart(text=message.text_content() or "")
                    ]
                }
            )
            for message in original.messages
        ]
        assert any(
            "TARGET WORKSPACE INSTRUCTIONS" in message.text_content()
            for message in resolutions[0].system_messages
        )
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_daemon_interactive(
            CLIArgs(root=str(tmp_path)),
            resume_id="saved",
            relocate=target,
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 0
    )
    assert store.load("saved").active_skills == [historical_skill]


@pytest.mark.parametrize("fail", [False, True])
def test_owned_agent_and_provider_close_before_lease_release(
    tmp_path: Path, monkeypatch, terminal, fail: bool
) -> None:
    store = terminal.store
    store.save("saved", [], root=tmp_path, title="Fixture")
    cleanup = []
    primary = ReplyProvider()
    entered, release = Event(), Event()

    class Fork(ReplyProvider):
        def complete(self, messages, tools):
            entered.set()
            assert release.wait(timeout=5)
            if fail:
                raise ValueError("failed during persistence")
            return super().complete(messages, tools)

        def close(self):
            with pytest.raises(WorkspaceBusy):
                with workspace_lease(store, "saved", exclusive=True):
                    pass
            cleanup.append("provider")

    class Primary(ReplyProvider):
        def fork_for_turn(self):
            return Fork()

    primary = Primary()
    terminal.factory = lambda _: RuntimeAgent(primary, [], tool_root=tmp_path)
    close = RuntimeAgent.close

    def close_agent(self):
        if isinstance(self.provider, Fork) and not self.closed:
            with pytest.raises(WorkspaceBusy):
                with workspace_lease(store, "saved", exclusive=True):
                    pass
            cleanup.append("agent")
        close(self)

    monkeypatch.setattr(RuntimeAgent, "close", close_agent)
    backend = terminal.backend("saved", root=tmp_path)
    turn = backend.submit("hello", user_message=None)
    assert entered.wait(timeout=3)
    if not fail:
        backend.interrupt()
    release.set()
    list(turn)
    assert isinstance(turn.result(), TurnFailure if fail else TurnStopped)
    eventually(lambda: cleanup == ["agent", "provider"])
    backend.connection.session_request("POST", "/drain", params={"timeoutMs": 5000})
    with workspace_lease(store, "saved", exclusive=True):
        pass


def test_headless_rechecks_workspace_after_agent_construction(
    tmp_path: Path, terminal
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    calls = []

    class Provider(ReplyProvider):
        def complete(self, messages, tools):
            calls.append(messages)
            return super().complete(messages, tools)

    def factory(_record):
        agent = RuntimeAgent(Provider(), [], tool_root=root)
        root.rmdir()
        return agent

    terminal.factory = factory
    assert (
        run_daemon_cli(
            CLIArgs(root=str(root), prompt="hello", headless=True),
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 1
    )
    assert not calls
    # HTTP creation is durable before startup; it contains no executed history.
    records = terminal.store.list()
    assert len(records) == 1
    assert terminal.store.load(records[0].id).messages == []


@pytest.mark.parametrize("mode", ["basic", "prompt"])
def test_deleted_workspace_reports_turn_failure_without_executing(
    tmp_path: Path, monkeypatch, terminal, mode: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    backend = terminal.backend("saved", root=root)
    active = session_for(backend)
    root.rmdir()
    agent = backend
    prepare = Mock(side_effect=AssertionError("forked provider before validation"))
    runtime = terminal.app.state.runtime_registry.get_if_loaded(active.id)
    monkeypatch.setattr(runtime.execution, "prepare", prepare)
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
            agent=agent,
            user_message=None,
            handle_outcome=lambda turn_id, outcome: results.put(outcome),
            turn_renderer_factory=Mock(),
        )
    outcome = results.get(timeout=1)
    assert isinstance(outcome, TurnFailure)
    assert "--relocate" in str(outcome.error)
    assert "--relocate" in str(outcome.error)
    assert terminal.store.load(active.id).messages == []
    prepare.assert_not_called()


def test_missing_workspace_keeps_queue_and_restores_submitted_text(
    tmp_path: Path,
    terminal,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    backend = terminal.backend("saved", root=root)
    active = session_for(backend)
    state = PromptCliState(messages=[], pending_prompts=[])
    lock = Lock()
    queued = PendingPrompt("queued work")
    assert queue_prompt_submission(
        consumed_images=[],
        state=state,
        state_lock=lock,
        active_session=active,
        prompt=queued,
    )
    before = load_prompt_queue_state(active)
    root.rmdir()
    assert dequeue_prompt(state=state, state_lock=lock, active_session=active) is None
    assert [(p.id, p.prompt) for p in state.pending_prompts] == [
        (queued.id, queued.prompt)
    ]
    assert (
        backend.connection.session_request("GET")["data"]["workspace"]["status"]
        == "missing"
    )
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
    assert dequeue_prompt(state=state, state_lock=lock, active_session=active) is None
    assert [p.id for p in backend.load_queue().prompts] == [queued.id]
    from .daemon_queue_support import wake

    wake(active)
    assert backend.load_queue().prompts == []
    assert [m.text_content() for m in backend.messages() if m.role == "user"] == [
        queued.prompt
    ]


@pytest.mark.parametrize("command", ["/fork", "/new"])
def test_deleted_workspace_session_switch_is_recoverable(
    tmp_path: Path, terminal, command: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    backend = terminal.backend("saved", root=root)
    active = session_for(backend)
    root.rmdir()
    stream = CaptureStream()
    handled, messages, updated = handle_slash_command(
        command,
        agent=backend,
        active_session=active,
        messages=[],
        console=build_console(stream),
    )
    assert handled
    assert messages == []
    assert updated is active
    assert "--relocate" in stream.getvalue()
    assert [record.id for record in terminal.store.list()] == [active.id]


@pytest.mark.parametrize("prompt", ["/new", "ordinary prompt"])
def test_prompt_submission_executor_retains_new_session_lease(
    tmp_path: Path, terminal, prompt: str
) -> None:
    entered, release = Event(), Event()

    class Provider(ReplyProvider):
        def complete(self, messages, tools):
            entered.set()
            assert release.wait(timeout=5)
            return super().complete(messages, tools)

    terminal.factory = lambda _: RuntimeAgent(Provider(), [], tool_root=tmp_path)
    backend = terminal.backend("initial", root=tmp_path)
    turns = []

    def submit(_prompt: str, _action: str) -> None:
        if _prompt == "/new":
            backend.new()
        backend.title("Fixture", [])
        turns.append(backend.submit("hello", user_message=None))
        assert entered.wait(timeout=3)
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(terminal.store, backend.session.id, exclusive=True):
                pass

    config = Mock()
    config.process_submission = submit
    lifecycle = PersistentPromptLifecycle(config)
    try:
        asyncio.run(lifecycle._process_submission(_Submission(prompt, "steer")))
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(terminal.store, backend.session.id, exclusive=True):
                pass
    finally:
        release.set()
        lifecycle._submission_executor.shutdown(wait=True)
    list(turns[0])
    assert isinstance(turns[0].result(), TurnSuccess)
    backend.connection.session_request("POST", "/drain", params={"timeoutMs": 5000})
    with workspace_lease(terminal.store, backend.session.id, exclusive=True):
        pass


def test_retired_turn_keeps_lease_until_detached_cleanup(
    tmp_path: Path, terminal
) -> None:
    release, waiting, finished = Event(), Event(), Event()

    class Fork(ReplyProvider):
        def complete(self, messages, tools):
            raise ValueError("retire failed turn")

        def close(self):
            waiting.set()
            assert release.wait(timeout=5)
            finished.set()

    provider = ReplyProvider()

    class Primary(ReplyProvider):
        def fork_for_turn(self):
            return Fork()

    provider = Primary()
    terminal.factory = lambda _: RuntimeAgent(provider, [], tool_root=tmp_path)
    terminal.store.save("saved", [], root=tmp_path, title="Fixture")
    backend = terminal.backend("saved", root=tmp_path)
    turn = backend.submit("hello", user_message=None)
    try:
        assert waiting.wait(timeout=3)
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(terminal.store, "saved", exclusive=True):
                pass
    finally:
        release.set()
        assert finished.wait(timeout=3)
    list(turn)
    assert isinstance(turn.result(), TurnFailure)
    backend.connection.session_request("POST", "/drain", params={"timeoutMs": 5000})
    with workspace_lease(terminal.store, "saved", exclusive=True):
        pass
