from __future__ import annotations

from PIL import Image
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from typing import cast

import pytest

from yoke.agent.models import (
    Message,
    MessageImageURLContentPart,
    MessageTextContentPart,
)
from yoke.agent.state import conversation_entries_from_messages
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.basic import _start_basic_turn
from yoke.cli.interactive.common import (
    BasicCliState,
    PromptCliState,
    TurnSuccess,
    format_context_usage_text,
)
from yoke.cli.interactive.prompt.loop import (
    process_prompt_toolkit_prompt,
)
from yoke.cli.interactive.tree_selector import TreeSelectorResult
from yoke.cli.render import InteractiveRenderer, build_console
from yoke.cli.backend.startup import run_daemon_cli as run_cli
from yoke.agent.loop import RuntimeAgent
from tests.yoke.daemon.support import eventually
from .terminal_daemon_support import (
    ReplyProvider,
    session_for,
    terminal as terminal,
    terminal_daemon,
)
from yoke.cli.runtime.base import ActiveSessionRef
from yoke.session import SessionStore

from .support import CaptureStream


def test_cli_runs_headless_prompt(capsys, terminal) -> None:
    exit_code = run_cli(CLIArgs(prompt="hello world", headless=True))
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "synthetic response"


def test_headless_cli_generates_title_without_blocking_user_turn(
    tmp_path, monkeypatch, terminal
) -> None:
    title_started, response_written, user_turn_started = Event(), Event(), Event()
    title_messages = []

    def generate(_factory, _record, messages):
        title_messages.extend(message.plain_text_content or "" for message in messages)
        title_started.set()
        assert response_written.wait(timeout=5)
        return "Generated Background Title"

    class ConcurrentProvider(ReplyProvider):
        def complete(self, messages, tools):
            assert title_started.wait(timeout=5)
            user_turn_started.set()
            return super().complete(messages, tools)

    class SignalingOutput(CaptureStream):
        def write(self, text: str) -> int:
            written = super().write(text)
            if "synthetic response" in self.getvalue():
                response_written.set()
            return written

    terminal.factory = lambda record: RuntimeAgent(
        ConcurrentProvider(), [], tool_root=Path(record.root)
    )
    monkeypatch.setattr("yoke.http.runtime.title.generate_http_session_title", generate)
    output = SignalingOutput()
    exit_code = run_cli(
        CLIArgs(prompt="Reply with exactly OK.", headless=True, root=str(tmp_path)),
        stdout=output,
        stderr=CaptureStream(),
    )
    assert exit_code == 0
    assert user_turn_started.is_set()
    assert title_messages == ["Reply with exactly OK."]
    eventually(
        lambda: (
            SessionStore().list(root=tmp_path)[0].title == "Generated Background Title"
        )
    )


def test_seeded_interactive_cli_does_not_wait_for_title_generation(
    tmp_path: Path,
    monkeypatch,
    terminal,
) -> None:
    def fail(*_args, **_kwargs) -> None:
        pytest.fail("interactive startup must leave title generation to the turn loop")

    observed: dict[str, object] = {}

    def fake_interactive(_args, _agent, messages, *, active_session, **_kwargs):
        observed["messages"] = list(messages)
        observed["title"] = active_session.title
        return 0

    monkeypatch.setattr("yoke.session.title.generate_session_title", fail)
    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", fake_interactive)

    exit_code = run_cli(
        CLIArgs(prompt="start useful work", root=str(tmp_path)),
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert observed["title"] is None
    messages = observed["messages"]
    assert isinstance(messages, list)
    assert all(isinstance(message, Message) for message in messages)
    typed_messages = cast(list[Message], messages)
    assert [message.plain_text_content for message in typed_messages] == [
        "start useful work"
    ]


def test_basic_interactive_turn_generates_title_without_blocking_turn(
    tmp_path, monkeypatch, terminal
) -> None:
    title_started, release_title = Event(), Event()

    def generate(*_args, **_kwargs):
        title_started.set()
        assert release_title.wait(timeout=5)
        return "Generated Interactive Title"

    class ConcurrentProvider(ReplyProvider):
        def complete(self, messages, tools):
            assert title_started.wait(timeout=5)
            release_title.set()
            return super().complete(messages, tools)

    terminal.factory = lambda record: RuntimeAgent(
        ConcurrentProvider(), [], tool_root=Path(record.root)
    )
    monkeypatch.setattr("yoke.http.runtime.title.generate_http_session_title", generate)
    backend = terminal.backend("basic-title", root=tmp_path)
    result_queue = Queue()
    thread = _start_basic_turn(
        "normal interactive prompt",
        state=BasicCliState(messages=[], pending_prompts=[]),
        active_session=session_for(backend),
        agent=backend,
        stderr=CaptureStream(),
        renderer=InteractiveRenderer(CaptureStream()),
        result_queue=result_queue,
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(result_queue.get_nowait(), TurnSuccess)
    eventually(
        lambda: (
            SessionStore().load("basic-title").title == "Generated Interactive Title"
        )
    )


def test_session_title_worker_is_deduplicated(tmp_path, monkeypatch, terminal) -> None:
    started, release = Event(), Event()
    calls = []

    def generate(*_args, **_kwargs):
        calls.append("title")
        started.set()
        assert release.wait(timeout=5)
        return "One Generated Title"

    monkeypatch.setattr("yoke.http.runtime.title.generate_http_session_title", generate)
    backend = terminal.backend("deduplicated-title", root=tmp_path)
    first = backend.submit(
        "first prompt",
        [],
        entries=[],
        stop_event=Event(),
        user_message=None,
        checkpoint=lambda *_: None,
    )
    try:
        assert started.wait(timeout=5)
        list(first)
        assert isinstance(first.result(), TurnSuccess)
        second = backend.submit(
            "second prompt",
            [],
            entries=[],
            stop_event=Event(),
            user_message=None,
            checkpoint=lambda *_: None,
        )
        list(second)
        assert isinstance(second.result(), TurnSuccess)
        assert calls == ["title"]
    finally:
        release.set()
    eventually(
        lambda: SessionStore().load("deduplicated-title").title == "One Generated Title"
    )


def test_prompt_state_adopts_provider_effort_after_model_command(
    tmp_path, monkeypatch, terminal
) -> None:
    backend = terminal.backend("effort", root=tmp_path)
    terminal.store.set_selection(
        "effort",
        provider_name=None,
        model_id=None,
        reasoning_effort="medium",
        context_window_tokens=None,
    )
    backend.refresh_session()
    active_session = session_for(backend)
    state = PromptCliState(messages=[], pending_prompts=[], thinking_effort="medium")

    def switch_model(*_args, **_kwargs):
        terminal.store.set_selection(
            "effort",
            provider_name=None,
            model_id=None,
            reasoning_effort="thinking",
            context_window_tokens=None,
        )
        backend.refresh_session()
        return True, [], active_session

    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.loop.handle_slash_command", switch_model
    )
    process_prompt_toolkit_prompt(
        "/model",
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(active_session),
        scrollback_console=build_console(CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
    )
    assert state.thinking_effort == "thinking"


def test_headless_cli_propagates_session_usage_attribution(tmp_path, terminal) -> None:
    from yoke.ai.providers.usage_context import current_usage_metric_context

    contexts = []

    class AttributionProvider(ReplyProvider):
        def complete(self, messages, tools):
            context = current_usage_metric_context()
            if context.call_kind != "session_title":
                contexts.append(context)
            return super().complete(messages, tools)

    terminal.factory = lambda record: RuntimeAgent(
        AttributionProvider(), [], tool_root=Path(record.root)
    )
    exit_code = run_cli(
        CLIArgs(prompt="CLI attribution title", headless=True, root=str(tmp_path)),
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )
    assert exit_code == 0
    assert len(contexts) == 1
    context = contexts[0]
    assert context.surface == "http"
    assert context.session_id is not None
    assert context.session_id.startswith("20")
    assert context.session_title == "CLI attribution title"


def test_cli_closes_runtime_it_constructs(tmp_path, monkeypatch) -> None:
    agents = []

    def factory(record):
        agent = RuntimeAgent(ReplyProvider(), [], tool_root=Path(record.root))
        agents.append(agent)
        return agent

    with terminal_daemon(monkeypatch, factory):
        exit_code = run_cli(
            CLIArgs(prompt="hello", headless=True, root=str(tmp_path)),
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        assert exit_code == 0
    assert agents
    assert all(agent.closed for agent in agents)


def test_cli_reads_headless_prompt_from_stdin(monkeypatch, terminal) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    exit_code = run_cli(
        CLIArgs(headless=True),
        input_func=lambda _="": "prompt from stdin\n",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue().strip() == "synthetic response"
    assert stderr.getvalue() == ""


def test_cli_headless_accepts_image_attachments(
    tmp_path: Path, monkeypatch, terminal
) -> None:
    session_dir = tmp_path / "sessions"
    image_path = tmp_path / "tiny.png"
    Image.new("RGB", (1, 1), color="red").save(image_path)
    seen_user_messages = []

    class ImageProvider(ReplyProvider):
        supports_image_inputs = True
        max_images_per_message = None

        def complete(self, messages, tools):
            seen_user_messages.append(messages[-1])
            return Message.assistant("image response")

    terminal.factory = lambda record: RuntimeAgent(
        ImageProvider(), [], tool_root=Path(record.root)
    )
    title_messages: list[Message] = []

    def generate(_factory, _record, messages) -> str:
        title_messages.extend(message.model_copy(deep=True) for message in messages)
        return "Describe Tiny Image"

    monkeypatch.setattr("yoke.http.runtime.title.generate_http_session_title", generate)

    exit_code = run_cli(
        CLIArgs(
            prompt="describe [tiny.png] please",
            headless=True,
            images=(str(image_path),),
            session="image-demo",
            root=str(tmp_path),
        ),
    )

    assert exit_code == 0
    assert len(seen_user_messages) == 1
    assert seen_user_messages[0].text_content() == "describe [tiny.png] please"
    content = seen_user_messages[0].content
    assert isinstance(content, list)
    text_part = content[0]
    image_part = content[1]
    assert isinstance(text_part, MessageTextContentPart)
    assert text_part.text == "describe [tiny.png] please"
    assert isinstance(image_part, MessageImageURLContentPart)
    assert image_part.image_url.url.startswith("data:image/png;base64,")
    assert image_part.label == "[Image #1]"

    stored = SessionStore(session_dir).load("image-demo")
    stored_messages = stored.messages
    assert stored_messages[0].text_content() == "describe [tiny.png] please"
    stored_content = stored_messages[0].content
    assert isinstance(stored_content, list)
    stored_text_part = stored_content[0]
    stored_image_part = stored_content[1]
    assert isinstance(stored_text_part, MessageTextContentPart)
    assert stored_text_part.text == "describe [tiny.png] please"
    assert isinstance(stored_image_part, MessageImageURLContentPart)
    assert stored_image_part.image_url.url.startswith("data:image/png;base64,")
    assert stored_image_part.label == "[Image #1]"
    eventually(
        lambda: (
            SessionStore(session_dir).load("image-demo").title == "Describe Tiny Image"
        )
    )
    assert len(title_messages) == 1
    assert title_messages[0].has_image_inputs()


def test_cli_requires_prompt_in_headless_mode(monkeypatch, terminal) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    exit_code = run_cli(
        CLIArgs(headless=True),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert (
        "Headless mode requires --prompt or prompt text from stdin."
        in stderr.getvalue()
    )


def test_tree_navigation_reprints_history_after_state_update(
    tmp_path: Path,
    monkeypatch,
    terminal,
) -> None:
    messages = [
        Message.user("first"),
        Message.assistant("answer"),
        Message.user("second"),
        Message.assistant("second answer"),
    ]
    terminal.store.save(
        "tree-navigation",
        messages,
        conversation_entries=conversation_entries_from_messages(messages),
        root=tmp_path,
    )
    backend = terminal.backend("tree-navigation", root=tmp_path)
    active_session = session_for(backend)
    assistant_entry = terminal.store.load(active_session.id).conversation_entries[1]
    state = PromptCliState(
        messages=backend.messages(),
        pending_prompts=[],
    )
    active_session_ref = ActiveSessionRef(active_session)
    stdout = CaptureStream()
    console = build_console(stdout)
    monkeypatch.setattr(
        "yoke.cli.interactive.tree_command.select_tree_entry_interactive",
        lambda *_args, **_kwargs: TreeSelectorResult(
            "select",
            assistant_entry.id,
        ),
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.tree_command._ask_branch_summary_choice",
        lambda: (False, None),
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.loop.print_session_scrollback",
        lambda _console, messages: _console.print(
            "replayed " + ",".join(message.role for message in messages)
        ),
    )

    updated_session = process_prompt_toolkit_prompt(
        "/tree",
        agent=backend,
        state=state,
        active_session_ref=active_session_ref,
        scrollback_console=console,
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
        on_editor_text=lambda _text: None,
    )

    assert updated_session.record.leaf_id is not None
    assert [message.role for message in state.messages] == [
        "user",
        "assistant",
    ]
    assert "replayed user,assistant" in stdout.getvalue()
    assert "Navigated to selected point." in stdout.getvalue()
