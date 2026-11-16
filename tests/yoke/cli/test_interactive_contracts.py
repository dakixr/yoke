"""Shared command, binding and turn contracts for both interactive front ends."""

from __future__ import annotations

import io
from dataclasses import replace
from queue import Queue
from threading import Event, Lock, Thread
from unittest.mock import Mock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import _parse_key

from yoke.agent.loop import AgentStoppedError
from yoke.agent.models import Message
from yoke.cli.config.resources import close_quietly
from yoke.cli.interactive import commands
from yoke.agent.loop import RuntimeAgent
from yoke.cli.interactive.basic import _start_basic_turn
from yoke.cli.interactive.command_host import CommandCallbacks
from yoke.cli.interactive.common import (
    BasicCliState,
    PromptCliState,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.completion import SlashCommandCompleter
from yoke.cli.interactive.events import WakeQueue
from yoke.cli.interactive.prompt import keys
from yoke.cli.interactive.prompt.lifecycle import (
    _normalize_submitted_prompt,
    _requires_terminal_control,
)
from yoke.cli.interactive.prompt.loop import process_prompt_toolkit_prompt
from yoke.cli.interactive.prompt.turns import run_prompt_turn
from yoke.cli.interactive.seeding import seed_session
from yoke.cli.render import InteractiveRenderer, build_console
from yoke.cli.runtime.base import ActiveSessionRef
from yoke.session.workspace import WorkspaceConflict

from .terminal_daemon_support import ReplyProvider, session_for, terminal as terminal


@pytest.mark.parametrize("command", commands.SLASH_COMMANDS, ids=lambda item: item.name)
def test_registry_dispatch_usage_completion_and_lifecycle_agree(
    tmp_path, monkeypatch, command, terminal
):
    backend = terminal.backend("contracts", root=tmp_path)
    session = session_for(backend)
    output = io.StringIO()
    handler = Mock(return_value=(True, [], session))
    entry = replace(command, handler=handler)
    for name in (entry.name, *entry.aliases):
        monkeypatch.setitem(commands.COMMANDS_BY_NAME, name, entry)
        text = f" {name.upper()} "
        assert commands.find_command(text) is entry
        assert _requires_terminal_control(text) is entry.needs_terminal
        assert _normalize_submitted_prompt(text) == (
            text if entry.takes_args else text.strip()
        )
        args = " example" if entry.min_args else ""
        assert commands.handle_slash_command(
            name + args,
            agent=backend,
            active_session=session,
            messages=[],
            console=build_console(output),
        )[0]
        handler.assert_called()
    if not entry.takes_args:
        handler.reset_mock()
        handled = commands.handle_slash_command(
            entry.name + " unexpected",
            agent=backend,
            active_session=session,
            messages=[],
            console=build_console(output),
        )[0]
        handler.assert_not_called()
        if entry.rejects_args:
            assert f"Usage: {entry.usage_text}" in output.getvalue()
        else:
            # Extra text after an argument-less command is a model prompt.
            assert handled is False
    if entry.min_args:
        handler.reset_mock()
        commands.handle_slash_command(
            entry.name,
            agent=backend,
            active_session=session,
            messages=[],
            console=build_console(output),
        )
        handler.assert_not_called()
        assert f"Usage: {entry.usage_text}" in output.getvalue()
    completions = SlashCommandCompleter().get_completions(
        Document("/"), CompleteEvent()
    )
    assert entry.name in {item.text for item in completions}


def test_help_aliases_unknown_commands_and_explicit_basic_capabilities(
    tmp_path, terminal
):
    backend = terminal.backend("contracts", root=tmp_path)
    session = session_for(backend)
    output = io.StringIO()
    host = CommandCallbacks(basic_mode=True)
    for command in ("/help", "?", "/tree", "/pin argument"):
        assert commands.handle_slash_command(
            command,
            agent=backend,
            active_session=session,
            messages=[],
            console=build_console(output),
            host=host,
        )[0]
    text = output.getvalue()
    assert all(
        entry.name in text
        for entry in commands.SLASH_COMMANDS
        if entry.available_in_basic
    )
    assert "Basic mode shortcuts:" in text
    assert commands.handle_slash_command(
        "/unknown",
        agent=backend,
        active_session=session,
        messages=[],
        console=build_console(output),
        host=host,
    ) == (False, [], session)
    assert "Unknown command" not in output.getvalue()
    assert "/tree is only available in the prompt-toolkit TUI." in text
    assert "Usage: /pin" in text


def test_compaction_dispatch_uses_host_capability(tmp_path, monkeypatch, terminal):
    output = io.StringIO()
    start = Mock(return_value=True)
    sync_compact = Mock(side_effect=AssertionError("must use host compaction"))
    backend = terminal.backend("compaction", root=tmp_path)
    monkeypatch.setattr(backend, "compact", sync_compact)
    commands.handle_slash_command(
        "/compact",
        agent=backend,
        active_session=session_for(backend),
        messages=[],
        console=build_console(output),
        host=CommandCallbacks(start_compaction=start),
    )
    start.assert_called_once_with()
    sync_compact.assert_not_called()


def test_shortcut_help_uses_only_registered_platform_bindings():
    bindings = KeyBindings()
    keys.register_prompt_toolkit_key_bindings(
        bindings,
        state=PromptCliState([], []),
        stop_active_turn=lambda: False,
        remove_last_image=lambda: None,
        cycle_thinking_effort=lambda: None,
        update_status=lambda _text: None,
        request_clipboard_paste=lambda *_args: None,
    )
    notice = keys.shortcut_notice()
    for binding in keys.BINDINGS.values():
        assert binding.help in notice
        for sequence in binding.sequences:
            assert bindings.get_bindings_for_keys(
                tuple(_parse_key(key) for key in sequence)
            )
    assert ("`Shift+Enter`" in notice) != ("`Esc` then `Enter`" in notice)
    output = io.StringIO()
    InteractiveRenderer(output).print_intro()
    assert keys.shortcut_notice(basic=True) in output.getvalue()


@pytest.mark.parametrize("supports_shift", [False, True])
def test_newline_binding_selects_platform_supported_sequence(
    monkeypatch, supports_shift
):
    def add(_self, key):
        assert key == "s-enter"
        if not supports_shift:
            raise ValueError("unsupported")

    monkeypatch.setattr(KeyBindings, "add", add)
    binding = keys._newline_binding()
    assert binding.sequences == (
        ("c-j",),
        ("s-enter",) if supports_shift else ("escape", "enter"),
    )
    assert ("Shift+Enter" in binding.help) is supports_shift


@pytest.mark.parametrize("state_type", [BasicCliState, PromptCliState])
def test_seed_session_retains_original_text_and_image_blocks(state_type):
    message = Message.user(
        [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aA=="}},
        ]
    )
    state = state_type([Message.assistant("history"), message], [])
    received = []
    seed_session(
        state,
        replay_session=False,
        replay_messages=None,
        replay_notice=None,
        console=build_console(io.StringIO()),
        start_turn=lambda *args: received.append(args),
    )
    prompt, seeded = received[0]
    assert prompt == message.display_text_content()
    assert seeded.model_dump_json() == message.model_dump_json()
    assert seeded is not message
    assert state.messages == [Message.assistant("history")]


@pytest.mark.parametrize("basic", [False, True], ids=["prompt", "basic"])
@pytest.mark.parametrize(
    "error", [AgentStoppedError("stopped"), WorkspaceConflict("session", "root")]
)
def test_both_frontends_share_preparation_and_stopped_workspace_outcomes(
    tmp_path, monkeypatch, basic, error, terminal
):
    backend = terminal.backend("contracts", root=tmp_path)
    session = session_for(backend)
    terminal.store.set_title(session.id, "Named fixture")
    prepared, executed = [], []
    fork, run = RuntimeAgent.fork, RuntimeAgent.run

    class OutcomeProvider(ReplyProvider):
        def complete(self, messages, tools):
            del messages, tools
            raise error

    terminal.factory = lambda _record: RuntimeAgent(
        OutcomeProvider(), [], tool_root=tmp_path
    )

    def prepare(self, **kwargs):
        isolated = fork(self, **kwargs)
        prepared.append(isolated)
        return isolated

    def execute(self, *args, **kwargs):
        executed.append(self)
        return run(self, *args, **kwargs)

    monkeypatch.setattr(RuntimeAgent, "fork", prepare)
    monkeypatch.setattr(RuntimeAgent, "run", execute)
    outcomes: Queue[TurnSuccess | TurnFailure | TurnStopped] = Queue()
    renderer = InteractiveRenderer(io.StringIO())
    if basic:
        worker = _start_basic_turn(
            "test",
            state=BasicCliState([], []),
            active_session=session,
            agent=backend,
            stderr=io.StringIO(),
            renderer=renderer,
            result_queue=outcomes,
        )
        worker.join(timeout=3)
        assert not worker.is_alive()
    else:
        run_prompt_turn(
            turn_id=1,
            prompt="test",
            state=PromptCliState([], []),
            state_lock=Lock(),
            agent=backend,
            active_session=session,
            stop_event=Event(),
            user_message=None,
            handle_outcome=lambda _id, outcome: outcomes.put(outcome),
            turn_renderer_factory=lambda _id: renderer,
        )
    outcome = outcomes.get(timeout=1)
    assert isinstance(
        outcome, TurnStopped if isinstance(error, AgentStoppedError) else TurnFailure
    )
    assert outcome.agent is None
    assert len(prepared) == 1
    assert executed == prepared


def test_input_and_worker_queues_wake_the_same_waiter():
    wake = Event()
    inputs: WakeQueue[str] = WakeQueue(wake)
    results: WakeQueue[str] = WakeQueue(wake)
    for queue in (inputs, results):
        wake.clear()
        queue.put("ready")
        assert wake.wait(timeout=0.1)
        assert queue.get_nowait() == "ready"


def test_prompt_turn_tracking_sets_are_independent_defaults():
    first, second = PromptCliState([], []), PromptCliState([], [])
    first.abandoned_turn_ids.add(1)
    first.steered_turn_ids.add(2)
    assert not second.abandoned_turn_ids
    assert not second.steered_turn_ids


def test_close_quietly_supports_dynamic_adapters_and_preserves_required_failures():
    resource = Mock()
    close_quietly(resource)
    resource.close.assert_called_once_with()
    close_quietly(object())
    resource.close.side_effect = ValueError("close failed")
    close_quietly(resource)
    with pytest.raises(ValueError, match="close failed"):
        close_quietly(resource, suppress_errors=False)


def test_host_compaction_does_not_overwrite_a_fast_worker_result(tmp_path, terminal):
    state = PromptCliState([], [])
    backend = terminal.backend("contracts", root=tmp_path)
    session = session_for(backend)
    compacted = [Message.assistant("compacted conversation")]

    def compact():
        state.messages = compacted
        return Thread()

    process_prompt_toolkit_prompt(
        "/compact",
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(session),
        scrollback_console=build_console(io.StringIO()),
        state_lock=Lock(),
        update_status=lambda _text: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=Mock(),
        start_compaction=compact,
        steer_active_turn=Mock(),
        format_context_usage_text=lambda _usage: None,
    )
    assert state.messages is compacted


@pytest.mark.parametrize(
    "text",
    ["? why does this fail", "/new feature: add X", "/help me fix this", "/tree view"],
)
def test_argless_command_words_followed_by_text_are_model_prompts(text: str) -> None:
    from yoke.cli.interactive.commands import find_command

    assert find_command(text) is None


@pytest.mark.parametrize(
    "text,name",
    [
        ("?", "/shortcuts"),
        ("/new", "/new"),
        ("/pin extra", "/pin"),
        ("/ps now", "/ps"),
        ("/title Better name", "/title"),
    ],
)
def test_command_matching_keeps_usage_errors_for_strict_commands(
    text: str, name: str
) -> None:
    from yoke.cli.interactive.commands import find_command

    item = find_command(text)
    assert item is not None and item.name == name
