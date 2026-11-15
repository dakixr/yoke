"""Golden ACP wire projection for a representative observed turn."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from acp import schema as s

from yoke.acp.observation.agents import AgentRoster
from yoke.acp.process_monitor import _Observed
from yoke.acp.tools import ToolCallProjector
from yoke.acp.turns import emit


class Wire:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update(self, session_id: str, update: dict[str, Any]) -> None:
        notification = s.SessionNotification.model_validate(
            {"sessionId": session_id, "update": update}
        )
        self.updates.append(
            notification.model_dump(mode="json", by_alias=True, exclude_none=True)[
                "update"
            ]
        )


def test_representative_turn_wire_golden() -> None:
    async def scenario() -> list[dict[str, Any]]:
        wire = Wire()
        agent = SimpleNamespace(
            update=wire.update,
            native=SimpleNamespace(
                data=lambda *args, **kwargs: None,
                process_monitor=SimpleNamespace(track=lambda *args, **kwargs: None),
            ),
        )
        tools = ToolCallProjector()
        await emit(
            cast(Any, agent),
            "session",
            "session.message.updated",
            {
                "inputID": "input",
                "iteration": 1,
                "phase": "commentary",
                "content": "Working",
            },
            tools,
        )
        await emit(
            cast(Any, agent),
            "session",
            "session.tool.started",
            {
                "tool_call_id": "call",
                "tool_name": "read",
                "tool_arguments": '{"path":"README.md"}',
            },
            tools,
        )
        await emit(
            cast(Any, agent),
            "session",
            "session.tool.ended",
            {
                "tool_call_id": "call",
                "tool_name": "read",
                "ok": True,
                "result": {"content": "done"},
            },
            tools,
        )
        observed = _Observed(
            "session",
            7,
            {"processID": "proc", "command": "echo hi"},
            "call",
            wire.update,
        )
        await observed.publish(
            {
                "processID": "proc",
                "status": "running",
                "output": {"tail": "hello"},
                "elapsedMs": 1,
                "exitCode": None,
            }
        )
        roster = AgentRoster("session", wire.update)
        await roster.reconcile(
            [
                {
                    "schemaVersion": 1,
                    "agentId": "agent",
                    "runId": "run",
                    "sessionID": "session",
                    "parentRunId": None,
                    "parentAgentId": None,
                    "runtimeSessionID": 7,
                    "name": "Worker",
                    "provider": "fixture",
                    "model": "model",
                    "status": "running",
                    "observation": "live",
                    "startedAt": "2026-01-01T00:00:00Z",
                    "finishedAt": None,
                    "lastSeenAt": "2026-01-01T00:00:01Z",
                    "updatedAt": "2026-01-01T00:00:01Z",
                    "version": 1,
                    "lastToolName": None,
                    "errorType": None,
                }
            ]
        )
        return wire.updates

    assert asyncio.run(scenario()) == [
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "yoke:commentary:input:1",
            "content": {"type": "text", "text": "Working"},
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call",
            "title": "Read file · README.md",
            "kind": "read",
            "status": "in_progress",
            "rawInput": {"path": "README.md"},
            "locations": [],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call",
            "title": "Read file · README.md",
            "kind": "read",
            "status": "completed",
            "rawInput": {"path": "README.md"},
            "rawOutput": {"content": "done"},
            "locations": [],
            "content": [
                {
                    "type": "content",
                    "content": {"type": "text", "text": "done"},
                }
            ],
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "yoke-process:proc",
            "kind": "other",
            "title": "Monitoring echo hi",
            "status": "in_progress",
            "rawOutput": {
                "type": "yoke_process",
                "processId": "proc",
                "status": "running",
                "command": "echo hi",
                "exitCode": None,
                "elapsedMs": 1,
                "toolUseId": "call",
                "summary": "hello",
            },
            "content": [
                {
                    "type": "content",
                    "content": {"type": "text", "text": "Process is running.\nhello"},
                }
            ],
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "yoke-agent:agent",
            "kind": "other",
            "title": "Worker",
            "status": "in_progress",
            "rawOutput": {
                "type": "yoke_agent",
                "schemaVersion": 1,
                "agentId": "agent",
                "runId": "run",
                "sessionID": "session",
                "parentRunId": None,
                "parentAgentId": None,
                "runtimeSessionID": 7,
                "name": "Worker",
                "provider": "fixture",
                "model": "model",
                "status": "running",
                "observation": "live",
                "startedAt": "2026-01-01T00:00:00Z",
                "finishedAt": None,
                "lastSeenAt": "2026-01-01T00:00:01Z",
                "updatedAt": "2026-01-01T00:00:01Z",
                "version": 1,
                "lastToolName": None,
                "errorType": None,
            },
            "content": [
                {
                    "type": "content",
                    "content": {"type": "text", "text": "Agent running."},
                }
            ],
        },
    ]
