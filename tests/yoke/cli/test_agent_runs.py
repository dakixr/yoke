"""Agent snapshots through the shared CLI dispatch and both console paths."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import Lock
from unittest.mock import Mock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from yoke.agent.models import Message
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.cli.interactive.basic import run_basic_interactive_cli
from yoke.cli.interactive.common import (
    PromptCliState,
    SLASH_COMMANDS,
    handle_slash_command,
)
from yoke.cli.interactive.completion import SlashCommandCompleter
from yoke.cli.interactive.prompt.loop import process_prompt_toolkit_prompt

from .support import CaptureStream, FakeAgent, active_session_for


class Registry:
    def __init__(self, runs: list[dict[str, object]]) -> None:
        self.runs = runs
        self.reads = 0

    def snapshots(self) -> list[dict[str, object]]:
        self.reads += 1
        return self.runs

    def close(self) -> None:
        pass


class TrackingAgent(FakeAgent):
    def __init__(self, manager: CommandProcessManager) -> None:
        super().__init__()
        self.command_process_manager = manager


def run(agent_id: str, **values: object) -> dict[str, object]:
    return {
        "agentId": agent_id,
        "runId": f"run-{agent_id}",
        "name": "reviewer",
        "provider": "fake",
        "model": "test-model",
        "status": "running",
        "observation": "live",
        "startedAt": "2026-09-28T00:00:00+00:00",
        "version": 1,
        "lastToolName": "read_file",
        "typedUsage": {"toolUses": 4},
        **values,
    }


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch):
    manager = CommandProcessManager()
    registry = Registry([])
    monkeypatch.setattr(manager, "agent_runs", registry, raising=False)
    # Neither console path should inspect output, subscribe, or alter a process.
    forbidden = Mock(side_effect=AssertionError("Not a registry snapshot read"))
    for name in (
        "snapshots",
        "output_chunks",
        "observe_output",
        "write_input",
        "terminate",
        "subscribe",
        "exec_command",
        "exec_argv",
    ):
        monkeypatch.setattr(manager, name, forbidden)
    try:
        yield TrackingAgent(manager), registry
        forbidden.assert_not_called()
    finally:
        manager.close()


def test_agents_command_groups_latest_run_without_changing_state(
    tmp_path: Path,
    tracked,
) -> None:
    agent, registry = tracked
    registry.runs = [
        run("stable", name="old-name", status="completed", version=99),
        run("stale-id", observation="stale"),
        run("lost-id", observation="lost"),
        run("done-id", status="completed"),
        run(
            "stable",
            runId="new-run",
            name="latest-name",
            startedAt="2026-09-28T01:00:00+00:00",
            version=2,
        ),
    ]
    before = deepcopy(registry.runs)
    active = active_session_for(tmp_path)
    messages = [Message.user("private prompt")]
    stream = CaptureStream()
    inspector = Mock()
    handled, returned, session = handle_slash_command(
        " /AGENTS ",
        agent=agent,
        active_session=active,
        messages=messages,
        console=Console(file=stream, width=160, color_system=None),
        on_process_inspector=inspector,
    )
    output = stream.getvalue()
    assert handled and returned is messages and session is active
    assert "SDK agents: 1 running, 4 total" in output
    assert "latest-name" in output and "old-name" not in output
    assert "fake/test-model" in output and "read_file" in output and "4" in output
    assert "stale" in output and "lost" in output and "completed" in output
    assert "running" not in next(
        line for line in output.splitlines() if "stale-id" in line
    )
    assert "running" not in next(
        line for line in output.splitlines() if "lost-id" in line
    )
    assert "private prompt" not in output
    assert registry.runs == before and registry.reads == 1
    inspector.assert_not_called()


def test_agents_table_uses_literal_text_and_ignores_private_fields(
    tmp_path: Path, tracked
) -> None:
    agent, registry = tracked
    registry.runs = [
        run(
            "[red]id[/red]",
            name="[bold]name[/bold]\x1b[31m\x9d\a\r\u202e",
            model="[blue]model[/blue]",
            lastToolName="[green]tool[/green]",
            prompt="secret-prompt",
            command="secret-command",
            output="secret-output",
            environment={"TOKEN": "secret-token"},
            toolArguments={"key": "secret-key"},
        )
    ]
    stream = CaptureStream()
    handle_slash_command(
        "/agents",
        agent=agent,
        active_session=active_session_for(tmp_path),
        messages=[],
        console=Console(file=stream, width=240, color_system=None),
    )
    output = stream.getvalue()
    for literal in (
        "[bold]name[/bold]",
        "[red]id[/red]",
        "[blue]model[/blue]",
        "[green]tool[/green]",
        "\\u202e",
    ):
        assert literal in output
    for forbidden in ("\x1b", "\x9d", "\a", "\r", "\u202e", "secret-"):
        assert forbidden not in output


@pytest.mark.parametrize(
    "command", ["/agents extra", "/agents\textra", "/agents\nextra"]
)
def test_agents_rejects_arguments_without_reading(
    tmp_path: Path, tracked, command: str
) -> None:
    agent, registry = tracked
    stream = CaptureStream()
    handled, _, _ = handle_slash_command(
        command,
        agent=agent,
        active_session=active_session_for(tmp_path),
        messages=[],
        console=Console(file=stream),
    )
    assert handled and "Usage: /agents" in stream.getvalue()
    assert registry.reads == 0


def test_agents_empty_and_unavailable_are_distinct(tmp_path: Path, tracked) -> None:
    agent, registry = tracked
    for runner, expected in (
        (agent, "No SDK agent runs yet."),
        (FakeAgent(), "Agent tracking is unavailable"),
    ):
        stream = CaptureStream()
        handled, _, _ = handle_slash_command(
            "/agents",
            agent=runner,
            active_session=active_session_for(tmp_path),
            messages=[],
            console=Console(file=stream),
        )
        assert handled and expected in stream.getvalue()
    assert registry.reads == 1


def test_agents_is_in_shared_catalog_and_completions() -> None:
    assert next(item for item in SLASH_COMMANDS if item.name == "/agents").usage is None
    completions = list(
        SlashCommandCompleter().get_completions(Document("/ag"), CompleteEvent())
    )
    assert [item.text for item in completions] == ["/agents"]
    assert completions[0].start_position == -3
    assert "SDK agent" in completions[0].display_meta_text


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_agents_terminal_states_do_not_count_as_running(
    tmp_path: Path, tracked, status: str
) -> None:
    agent, registry = tracked
    registry.runs = [run("terminal", status=status, typedUsage=None)]
    stream = CaptureStream()
    handle_slash_command(
        "/agents",
        agent=agent,
        active_session=active_session_for(tmp_path),
        messages=[],
        console=Console(file=stream, width=160),
    )
    assert "SDK agents: 0 running, 1 total" in stream.getvalue()
    assert status in stream.getvalue()


def test_basic_cli_dispatches_agents_as_a_snapshot(tmp_path: Path, tracked) -> None:
    agent, registry = tracked
    registry.runs = [run("basic")]
    inputs = iter(["/agents", "exit"])
    stream = CaptureStream()

    def read_input(_prompt: object = "") -> str:
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError from None

    result = run_basic_interactive_cli(
        agent,
        [],
        active_session=active_session_for(tmp_path),
        input_func=read_input,
        stdout=stream,
        stderr=CaptureStream(),
    )
    assert result == 0 and "SDK agents: 1 running, 1 total" in stream.getvalue()
    assert agent.seen_history_lengths == [] and registry.reads == 1


def test_prompt_toolkit_dispatches_agents_without_starting_a_turn(
    tmp_path: Path, tracked
) -> None:
    agent, registry = tracked
    registry.runs = [run("prompt")]
    active = active_session_for(tmp_path)
    messages = [Message.user("previous")]
    state = PromptCliState(messages=messages, pending_prompts=[])
    forbidden = Mock(side_effect=AssertionError("Unexpected interactive action"))
    stream = CaptureStream()
    returned = process_prompt_toolkit_prompt(
        "/agents",
        state=state,
        agent=agent,
        active_session_ref={"active_session": active},
        scrollback_console=Console(file=stream, width=160),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=forbidden,
        start_turn=forbidden,
        steer_active_turn=forbidden,
        open_process_inspector=forbidden,
        format_context_usage_text=lambda _usage: None,
    )
    assert returned is active and state.messages is messages
    assert "SDK agents: 1 running, 1 total" in stream.getvalue()
    assert registry.reads == 1
    forbidden.assert_not_called()
