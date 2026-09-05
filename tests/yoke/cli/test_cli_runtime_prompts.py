from __future__ import annotations

from queue import Queue
from threading import Event
from threading import Lock
from threading import Thread
from types import SimpleNamespace
from typing import cast

from yoke.agent.models import Message
from yoke.agent.state import conversation_entries_from_messages
from yoke.cli.interactive.tree_selector import TreeSelectorResult
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.prompt.loop import (
    process_prompt_toolkit_prompt,
)

from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.cli.interactive.basic import _start_basic_turn
from yoke.cli.interactive.common import BasicCliState
from yoke.cli.interactive.common import TurnSuccess
from yoke.cli.render import InteractiveRenderer
from yoke.cli.runtime.lifetime import close_cli_owned_agent
from yoke.cli.runtime.lifetime import register_cli_owned_agent
from yoke.cli.runtime.session import save_active_session
from yoke.cli.runtime.title import start_session_title_generation
from yoke.cli.runtime.title import wait_for_session_title

import base64
from pathlib import Path

import pytest

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.main import CLIArgs, run_cli
from yoke.cli.render import build_console
from yoke.cli.runtime import create_active_session
from yoke.cli.session import SessionStore

from .support import CaptureStream, FakeAgent, ImageAwareAgent, active_session_for


def test_cli_runs_headless_prompt(capsys) -> None:
    exit_code = run_cli(CLIArgs(prompt="hello world", headless=True), agent=FakeAgent())

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "synthetic response"


def test_headless_cli_generates_title_without_blocking_user_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    title_started = Event()
    response_written = Event()
    user_turn_started = Event()
    title_messages: list[str] = []

    def generate(_agent, messages) -> str:
        title_messages.extend(message.plain_text_content or "" for message in messages)
        title_started.set()
        assert response_written.wait(timeout=2)
        return "Generated Background Title"

    class ConcurrentAgent(FakeAgent):
        def run(self, *args, **kwargs):
            assert title_started.wait(timeout=2)
            user_turn_started.set()
            return super().run(*args, **kwargs)

    class SignalingOutput(CaptureStream):
        def write(self, text: str) -> int:
            written = super().write(text)
            if "synthetic response" in self.getvalue():
                response_written.set()
            return written

    monkeypatch.setattr("yoke.cli.runtime.title.generate_session_title", generate)
    agent = ConcurrentAgent()

    output = SignalingOutput()
    exit_code = run_cli(
        CLIArgs(
            prompt="Reply with exactly OK.",
            headless=True,
            root=str(tmp_path),
        ),
        agent=agent,
        stdout=output,
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert user_turn_started.is_set()
    assert title_messages == ["Reply with exactly OK."]
    records = SessionStore().list(root=tmp_path)
    assert records[0].title == "Generated Background Title"


def test_seeded_interactive_cli_does_not_wait_for_title_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail(*_args, **_kwargs) -> None:
        pytest.fail("interactive startup must leave title generation to the turn loop")

    observed: dict[str, object] = {}

    def fake_interactive(_args, _agent, messages, *, active_session, **_kwargs):
        observed["messages"] = list(messages)
        observed["title"] = active_session.title
        return 0

    monkeypatch.setattr("yoke.cli.runtime.title.generate_session_title", fail)
    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", fake_interactive)

    exit_code = run_cli(
        CLIArgs(prompt="start useful work", root=str(tmp_path)),
        agent=FakeAgent(),
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
    tmp_path: Path,
    monkeypatch,
) -> None:
    title_started = Event()
    release_title = Event()

    def generate(*_args, **_kwargs) -> str:
        title_started.set()
        assert release_title.wait(timeout=2)
        return "Generated Interactive Title"

    class ConcurrentAgent(FakeAgent):
        def run(self, *args, **kwargs):
            assert title_started.wait(timeout=2)
            release_title.set()
            return super().run(*args, **kwargs)

    monkeypatch.setattr("yoke.cli.runtime.title.generate_session_title", generate)
    active_session = create_active_session(
        CLIArgs(root=str(tmp_path)),
        root=tmp_path,
    )
    save_active_session(active_session, [])
    result_queue: Queue = Queue()
    thread = _start_basic_turn(
        "normal interactive prompt",
        state=BasicCliState(messages=[], pending_prompts=[]),
        active_session=active_session,
        agent=ConcurrentAgent(),
        stderr=CaptureStream(),
        renderer=InteractiveRenderer(CaptureStream()),
        result_queue=result_queue,
    )

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert isinstance(result_queue.get_nowait(), TurnSuccess)
    wait_for_session_title(active_session)
    assert SessionStore().load(active_session.id).title == "Generated Interactive Title"


def test_session_title_worker_is_deduplicated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    started = Event()
    release = Event()
    calls = 0

    def generate(*_args, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return "One Generated Title"

    monkeypatch.setattr("yoke.cli.runtime.title.generate_session_title", generate)
    active_session = create_active_session(
        CLIArgs(root=str(tmp_path)),
        root=tmp_path,
    )
    save_active_session(active_session, [])
    agent = FakeAgent()

    first = start_session_title_generation(active_session, agent, "first prompt")
    assert started.wait(timeout=2)
    second = start_session_title_generation(active_session, agent, "second prompt")

    assert first is second
    assert calls == 1
    release.set()
    wait_for_session_title(active_session)
    assert SessionStore().load(active_session.id).title == "One Generated Title"


def test_prompt_state_adopts_provider_effort_after_model_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = FakeAgent(
        provider=SimpleNamespace(
            config=SimpleNamespace(reasoning_effort="medium"),
        )
    )
    active_session = active_session_for(tmp_path)
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        thinking_effort="medium",
    )

    def switch_model(*_args, **_kwargs):
        agent.provider.config.reasoning_effort = "thinking"
        return True, [], active_session

    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.loop.handle_slash_command",
        switch_model,
    )

    process_prompt_toolkit_prompt(
        "/model",
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session},
        scrollback_console=build_console(CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
    )

    assert state.thinking_effort == "thinking"


def test_headless_cli_propagates_session_usage_attribution(
    tmp_path: Path,
) -> None:
    from yoke.ai.providers.usage_context import (
        current_usage_metric_context,
    )

    class AttributionAgent(FakeAgent):
        observed_context = None

        def run(self, *args, **kwargs):
            self.observed_context = current_usage_metric_context()
            return super().run(*args, **kwargs)

    agent = AttributionAgent()

    exit_code = run_cli(
        CLIArgs(
            prompt="CLI attribution title",
            headless=True,
            root=str(tmp_path),
        ),
        agent=agent,
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert agent.observed_context is not None
    assert agent.observed_context.surface == "cli"
    session_id = agent.observed_context.session_id
    assert session_id is not None
    assert session_id.startswith("20")
    assert agent.observed_context.session_title == "CLI attribution title"


def test_cli_closes_runtime_it_constructs(tmp_path: Path, monkeypatch) -> None:
    class CloseTrackingAgent(FakeAgent):
        closed = False

        def close(self) -> None:
            self.closed = True

    agent = CloseTrackingAgent()
    monkeypatch.setattr(
        "yoke.cli.runtime.cli.build_cli_agent_from_args",
        lambda _args: SimpleNamespace(agent=agent, tool_report=None),
    )

    exit_code = run_cli(
        CLIArgs(prompt="hello", headless=True, root=str(tmp_path)),
        stdout=CaptureStream(),
        stderr=CaptureStream(),
    )

    assert exit_code == 0
    assert agent.closed is True


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


def test_cli_reads_headless_prompt_from_stdin(monkeypatch) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    exit_code = run_cli(
        CLIArgs(headless=True),
        agent=FakeAgent(),
        input_func=lambda _="": "prompt from stdin\n",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue().strip() == "synthetic response"
    assert stderr.getvalue() == ""


def test_cli_headless_accepts_image_attachments(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "sessions"
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="
        )
    )
    agent = ImageAwareAgent()
    title_messages: list[Message] = []

    def generate(_agent, messages) -> str:
        title_messages.extend(message.model_copy(deep=True) for message in messages)
        return "Describe Tiny Image"

    monkeypatch.setattr("yoke.cli.runtime.title.generate_session_title", generate)

    exit_code = run_cli(
        CLIArgs(
            prompt="describe [tiny.png] please",
            headless=True,
            images=(str(image_path),),
            session="image-demo",
            root=str(tmp_path),
        ),
        agent=agent,
    )

    assert exit_code == 0
    assert len(agent.seen_user_messages) == 1
    assert agent.seen_user_messages[0].text_content() == "describe [tiny.png] please"
    content = agent.seen_user_messages[0].content
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
    assert stored.title == "Describe Tiny Image"
    assert len(title_messages) == 1
    assert title_messages[0].has_image_inputs()


def test_cli_requires_prompt_in_headless_mode(monkeypatch) -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    exit_code = run_cli(
        CLIArgs(headless=True),
        agent=FakeAgent(),
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
) -> None:
    active_session = active_session_for(tmp_path)
    messages = [
        Message.user("first"),
        Message.assistant("answer"),
        Message.user("second"),
        Message.assistant("second answer"),
    ]
    active_session.store.save(
        active_session.id,
        messages,
        conversation_entries=conversation_entries_from_messages(messages),
        root=tmp_path,
    )
    active_session.record = active_session.store.load(active_session.id)
    assistant_entry = active_session.record.conversation_entries[1]
    state = PromptCliState(
        messages=list(active_session.record.messages),
        pending_prompts=[],
    )
    active_session_ref = {"active_session": active_session}
    stdout = CaptureStream()
    console = build_console(stdout)
    monkeypatch.setattr(
        "yoke.cli.interactive.slash_commands.select_tree_entry_interactive",
        lambda *_args, **_kwargs: TreeSelectorResult(
            "select",
            assistant_entry.id,
        ),
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.slash_commands._ask_branch_summary_choice",
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
        state=state,
        agent=FakeAgent(),
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
