from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from datetime import UTC
from datetime import datetime
from pathlib import Path

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import WriteStdinTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_types import CommandProcessResult
from yoke.agent.tools.command_process_types import CommandProcessSnapshot
from yoke.agent.tools.command_process_types import command_completion_event_id
from yoke.ai.providers.base import Provider


def _start_background_process(
    manager: CommandProcessManager,
    tmp_path: Path,
) -> CommandProcessResult:
    session_id = 1_000 + len(manager.completion_events())
    manager._record_completion_event(_completion_snapshot(session_id, tmp_path))
    return CommandProcessResult(
        session_id=session_id,
        exit_code=None,
        output="",
        wall_time_seconds=0.0,
        original_output_bytes=0,
    )


def _completion_snapshot(
    session_id: int,
    tmp_path: Path,
) -> CommandProcessSnapshot:
    return CommandProcessSnapshot(
        session_id=session_id,
        pid=2_000 + session_id,
        command="python -u -c <code>",
        cwd=tmp_path,
        tty=False,
        status="exited",
        started_at=datetime.now(UTC),
        elapsed_seconds=0.0,
        exit_code=0,
        output_tail="final line",
        original_output_bytes=10,
        retained_output_bytes=10,
    )


class _CompletedCommandProcessManager(CommandProcessManager):
    def write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_ms: int | None,
        cancel_requested,
    ) -> CommandProcessResult:
        del session_id, chars, yield_time_ms, cancel_requested
        return CommandProcessResult(
            session_id=None,
            exit_code=0,
            output="final line",
            wall_time_seconds=0.0,
            original_output_bytes=10,
        )


class NoticeRecordingProvider(Provider):
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        self.messages = [message.model_copy(deep=True) for message in messages]
        return Message.assistant("noticed")


def test_runtime_appends_completion_notice_before_next_model_call(
    tmp_path: Path,
) -> None:
    manager = CommandProcessManager()
    started = _start_background_process(manager, tmp_path)
    assert started.session_id is not None
    provider = NoticeRecordingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        command_process_manager=manager,
    )
    try:
        result = agent.run("Continue with the result")

        notice = provider.messages[-1]
        assert notice.role == "user"
        assert notice.plain_text_content is not None
        assert "Automatic command lifecycle notice" in notice.plain_text_content
        assert f'"session_id": {started.session_id}' in notice.plain_text_content
        assert '"exit_code": 0' in notice.plain_text_content
        assert "final line" in notice.plain_text_content
        assert "Do not poll these session IDs again" in notice.plain_text_content
        assert result.messages[-2] == notice
        notices = [
            message
            for message in result.messages
            if "Automatic command lifecycle notice"
            in (message.plain_text_content or "")
        ]
        assert notices == [notice]
    finally:
        agent.close()


def test_terminal_poll_suppresses_duplicate_completion_notice(
    tmp_path: Path,
) -> None:
    manager = _CompletedCommandProcessManager()
    provider = NoticeRecordingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[WriteStdinTool.bind(root=tmp_path)],
        command_process_manager=manager,
    )
    try:
        started = _start_background_process(manager, tmp_path)
        assert started.session_id is not None

        completed = (
            agent.tools["write_stdin"]
            .parse_arguments({"session_id": started.session_id})
            .execute()
        )
        events = manager.completion_events()

        assert completed["session_id"] is None
        assert command_completion_event_id(events[0]) in (
            agent._seen_command_completion_events
        )
        result = agent.run("Continue after the terminal poll")
        assert all(
            "Automatic command lifecycle notice"
            not in (message.plain_text_content or "")
            for message in result.messages
        )
    finally:
        agent.close()
