from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from dataclasses import replace
from datetime import UTC
from datetime import datetime
import json
from pathlib import Path
from typing import cast

import pytest

from yoke.agent.internal_events import is_command_completion_message
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.tools import ProcessReadTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_types import CommandProcessSnapshot
from yoke.agent.tools.command_process_types import (
    CommandProcessOutputPage,
    CommandProcessOutputChunk,
)
from yoke.agent.tools.command_process_types import command_completion_event_id
from yoke.agent.tools.command_process_types import MAX_COMPLETED_PROCESS_COUNT
from yoke.agent.tools.processes.cursor import ProcessPosition
from yoke.agent.tools.processes.observation import page
from yoke.ai.providers.base import Provider


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


class _ReadableCommandProcessManager(CommandProcessManager):
    def __init__(self, snapshot: CommandProcessSnapshot) -> None:
        super().__init__()
        self.process_snapshot = snapshot

    def observe_output(
        self, session_id: int, *, after_seq: int, limit: int
    ) -> tuple[CommandProcessSnapshot, CommandProcessOutputPage]:
        snapshot = self.process_snapshot
        assert session_id == snapshot.session_id
        return snapshot, CommandProcessOutputPage(
            chunks=(CommandProcessOutputChunk(seq=1, text=snapshot.output_tail),)
            if after_seq == 0 and limit
            else (),
            latest_seq=1,
            truncated_before_seq=0,
        )


class NoticeRecordingProvider(Provider):
    def __init__(self, *responses: Message) -> None:
        self.messages: list[Message] = []
        self.responses = iter(responses)

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        self.messages = [message.model_copy(deep=True) for message in messages]
        return next(self.responses, Message.assistant("noticed"))


def _process_read_call(session_id: int, cursor: str | None = None) -> Message:
    return Message(
        role="assistant",
        tool_calls=[
            ToolCall(
                id="read-process",
                function=ToolFunction(
                    name="process_read",
                    arguments=json.dumps(
                        {
                            "sessions": [{"session_id": session_id, "cursor": cursor}],
                            "wait_ms": 0,
                        }
                    ),
                ),
            )
        ],
    )


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    "output",
    ["", "final line", "large-output\n" * 2_000],
    ids=["empty", "normal", "large"],
)
def test_runtime_appends_completion_notice_before_next_model_call(
    tmp_path: Path,
    exit_code: int,
    output: str,
) -> None:
    manager = CommandProcessManager()
    snapshot = replace(
        _completion_snapshot(1_000, tmp_path),
        status="failed" if exit_code else "exited",
        exit_code=exit_code,
        output_tail=output,
        original_output_bytes=len(output.encode("utf-8")),
        retained_output_bytes=len(output.encode("utf-8")),
    )
    manager._record_completion_event(snapshot)
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
        payload = json.loads(notice.plain_text_content.split("\n", 2)[2])
        assert payload == {
            "completed_sessions": [
                {
                    "session_id": snapshot.session_id,
                    "status": snapshot.status,
                    "exit_code": exit_code,
                    "elapsed_seconds": snapshot.elapsed_seconds,
                    "command": snapshot.command,
                    "workdir": str(tmp_path),
                }
            ],
            "older_events_dropped": 0,
        }
        assert (
            "Use process_read with the last returned cursor"
            in notice.plain_text_content
        )
        assert manager.completion_events() == [snapshot]
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


def test_terminal_read_suppresses_duplicate_completion_notice(
    tmp_path: Path,
) -> None:
    snapshot = _completion_snapshot(1_000, tmp_path)
    manager = _ReadableCommandProcessManager(snapshot)
    provider = NoticeRecordingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[ProcessReadTool.bind(root=tmp_path)],
        command_process_manager=manager,
    )
    try:
        manager._record_completion_event(snapshot)

        completed = (
            agent.tools["process_read"]
            .parse_arguments({"sessions": [{"session_id": snapshot.session_id}]})
            .execute()
        )
        events = manager.completion_events()

        assert completed["reason"] == "completed"
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


def test_notice_then_cursor_read_delivers_output_once(tmp_path: Path) -> None:
    snapshot = _completion_snapshot(1_000, tmp_path)
    manager = _ReadableCommandProcessManager(
        replace(snapshot, status="running", exit_code=None)
    )
    # A launch can return a cursor before any output is included in its result.
    initial = page(manager, ProcessPosition(snapshot.session_id), budget=0)
    assert initial["running"] is True
    assert initial["output"] == ""
    manager.process_snapshot = snapshot
    manager._record_completion_event(snapshot)
    provider = NoticeRecordingProvider(
        _process_read_call(snapshot.session_id, initial["cursor"])
    )
    agent = RuntimeAgent(
        provider=provider,
        tools=[ProcessReadTool.bind(root=tmp_path)],
        command_process_manager=manager,
    )
    try:
        agent.run("Read the completed process")

        assert [message.role for message in provider.messages] == [
            "user",
            "user",
            "assistant",
            "tool",
        ]
        assert is_command_completion_message(provider.messages[1])
        assert (
            sum(
                (message.plain_text_content or "").count(snapshot.output_tail)
                for message in provider.messages
            )
            == 1
        )
        read = json.loads(provider.messages[-1].plain_text_content or "{}")
        item = read["items"][0]
        assert item["output"] == snapshot.output_tail
        assert item["running"] is False
        assert item["gap"] is False
        assert item["has_more_output"] is False
        # Notice delivery neither consumes retained output nor changes cursors.
        assert page(manager, ProcessPosition(snapshot.session_id), budget=100) == item
        assert manager.completion_events() == [snapshot]
    finally:
        agent.close()


def test_running_read_then_notice_does_not_repeat_output(tmp_path: Path) -> None:
    snapshot = _completion_snapshot(1_000, tmp_path)
    manager = _ReadableCommandProcessManager(
        replace(snapshot, status="running", exit_code=None)
    )
    provider = NoticeRecordingProvider(_process_read_call(snapshot.session_id))
    agent = RuntimeAgent(
        provider=provider,
        tools=[ProcessReadTool.bind(root=tmp_path)],
        command_process_manager=manager,
    )

    def finish_after_read(_context: AgentContext) -> None:
        # Finish after the running result is appended, without producing new bytes.
        if manager.process_snapshot.status == "running":
            manager.process_snapshot = snapshot
            manager._record_completion_event(snapshot)

    try:
        agent.run(
            "Read the running process", after_tool_result_appended=finish_after_read
        )

        assert [message.role for message in provider.messages] == [
            "user",
            "assistant",
            "tool",
            "user",
        ]
        assert is_command_completion_message(provider.messages[-1])
        assert manager.completion_events() == [snapshot]
        assert (
            sum(
                (message.plain_text_content or "").count(snapshot.output_tail)
                for message in provider.messages
            )
            == 1
        )
        read = json.loads(provider.messages[-2].plain_text_content or "{}")
        item = read["items"][0]
        assert item["output"] == snapshot.output_tail
        assert item["running"] is True
        assert item["has_more_output"] is False
        completed = (
            agent.tools["process_read"]
            .parse_arguments(
                {
                    "sessions": [
                        {"session_id": snapshot.session_id, "cursor": item["cursor"]}
                    ]
                }
            )
            .execute()
        )
        assert completed["reason"] == "completed"
        completed_items = cast(list[dict[str, object]], completed["items"])
        assert len(completed_items) == 1
        assert completed_items[0]["running"] is False
        assert completed_items[0]["output"] == ""
        assert completed_items[0]["cursor"] == item["cursor"]
    finally:
        agent.close()


def test_status_notices_preserve_batching_and_dropped_event_count(
    tmp_path: Path,
) -> None:
    manager = CommandProcessManager()
    for index in range(MAX_COMPLETED_PROCESS_COUNT + 1):
        manager._record_completion_event(_completion_snapshot(1_000 + index, tmp_path))
    provider = NoticeRecordingProvider()
    agent = RuntimeAgent(provider=provider, tools=[], command_process_manager=manager)
    try:
        agent.run("Check completed processes")
        notice = provider.messages[-1]
        assert is_command_completion_message(notice)
        payload = json.loads((notice.plain_text_content or "").split("\n", 2)[2])
        assert payload["older_events_dropped"] == 1
        assert [item["session_id"] for item in payload["completed_sessions"]] == list(
            range(1_001, 1_001 + MAX_COMPLETED_PROCESS_COUNT)
        )
        assert "final line" not in (notice.plain_text_content or "")

        agent.run("Continue")
        assert (
            sum(is_command_completion_message(message) for message in provider.messages)
            == 1
        )
    finally:
        agent.close()
