"""Stock ACP requests against a native runtime, including interactive clients."""

from __future__ import annotations

import asyncio

import httpx
from acp import schema as s
from acp.agent.router import build_agent_router

from yoke.acp.bridge import YokeAcpAgent
from tests.yoke.acp.test_native_input import Connection, ReceiptNative
from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for


async def catalog(*_args):
    return {
        "fixture:model": {
            "id": "fixture:model",
            "name": "Fixture",
            "reasoningEfforts": [],
            "defaultReasoningEffort": None,
            "images": True,
        }
    }, "fixture:model"


def test_standard_lifecycle_config_commands_and_notification_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("yoke.acp.bridge.discover", catalog)

    async def scenario():
        app = app_for(tmp_path, RecordingProvider())
        async with app.router.lifespan_context(app):
            native = ReceiptNative("http://fixture", "unused")
            await native.http.aclose()
            native.http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture/api/v1/",
                headers={"Authorization": "Bearer runtime-lifetime-secret"},
            )
            agent = YokeAcpAgent(native)
            connection = Connection()
            agent.on_connect(connection)
            router = build_agent_router(agent, use_unstable_protocol=True)
            try:
                initialized = await router(
                    "initialize",
                    {
                        "protocolVersion": 2,
                        "clientCapabilities": {"elicitation": {"form": {}}},
                    },
                    False,
                )
                assert (
                    initialized.agent_capabilities.session_capabilities.fork is not None
                )
                created = await router(
                    "session/new",
                    {
                        "cwd": str(tmp_path),
                        "mcpServers": [
                            {
                                "name": "t3-code",
                                "command": "/test/stdio",
                                "args": [],
                                "env": [
                                    {"name": "AUTHORIZATION", "value": "ephemeral"}
                                ],
                            }
                        ],
                    },
                    False,
                )
                assert initialized.auth_methods == []
                session_id = created.session_id
                assert created.modes.current_mode_id == "approval-required"
                command_update = next(
                    update
                    for update in connection.updates
                    if update["sessionUpdate"] == "available_commands_update"
                )
                assert "compact" in {
                    command["name"] for command in command_update["availableCommands"]
                }
                listed = await router("session/list", {"cwd": str(tmp_path)}, False)
                assert session_id in {item.session_id for item in listed.sessions}
                await router(
                    "session/set_mode",
                    {"sessionId": session_id, "modeId": "plan"},
                    False,
                )
                assert agent.options(session_id)[-1]["currentValue"] == "plan"
                for command in (
                    "/info",
                    "/ps",
                    "/agents",
                    "/mcp",
                    "/shortcuts",
                    "/title ACP QA",
                ):
                    response = await router(
                        "session/prompt",
                        {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": command}],
                        },
                        False,
                    )
                    assert response.stop_reason == "end_turn"
                assert (await native.data("GET", native.path(session_id)))[
                    "title"
                ] == "ACP QA"
                # Missed live events recover from the native pending-input lists.
                permission = app.state.human_input_service.create_permission(
                    session_id,
                    permission="write",
                    message="Allow the test?",
                    tool_call={
                        "toolCallId": "call-1",
                        "title": "write",
                        "kind": "edit",
                        "rawInput": {"path": "test.txt"},
                    },
                )

                async def approve(**kwargs):
                    assert kwargs["tool_call"]["toolCallId"] == "call-1"
                    assert kwargs["tool_call"]["locations"] == [
                        {"path": str(tmp_path / "test.txt")}
                    ]
                    return s.RequestPermissionResponse.model_validate(
                        {"outcome": {"outcome": "selected", "optionId": "allow"}}
                    )

                setattr(connection, "request_permission", approve)
                await agent.notifications.refresh(session_id)
                await asyncio.gather(*list(agent.notifications.requests.values()))
                assert (
                    app.state.human_input_service.wait_permission(
                        permission.id, 0
                    ).reply
                    == "allow"
                )
                question = app.state.human_input_service.create_question(
                    session_id, question="Choose", options=["one", "two"]
                )

                async def answer(**kwargs):
                    assert kwargs["mode"].session_id == session_id
                    return s.AcceptElicitationResponse(
                        action="accept", content={"answer": "two"}
                    )

                setattr(connection, "create_elicitation", answer)
                await agent.notifications.refresh(session_id)
                await asyncio.gather(*list(agent.notifications.requests.values()))
                assert app.state.human_input_service.wait_question(
                    question.id, 0
                ).answers == ["two"]
                await agent.notifications.event(
                    session_id, "session.compaction.started", {"operationID": "op-1"}
                )
                await agent.notifications.event(
                    session_id,
                    "session.compaction.failed",
                    {"operationID": "op-1", "error": "fixture"},
                )
                assert connection.updates[-1]["status"] == "failed"
                assert (
                    connection.updates[-2]["toolCallId"]
                    == connection.updates[-1]["toolCallId"]
                )
                await agent.notifications.event(
                    session_id,
                    "session.context.updated",
                    {"input_tokens": 123, "max_total_tokens": 1000},
                )
                assert connection.updates[-1] == {
                    "sessionUpdate": "usage_update",
                    "used": 123,
                    "size": 1000,
                }
                forked = await router(
                    "session/fork",
                    {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []},
                    False,
                )
                assert forked.session_id != session_id
                await router("session/close", {"sessionId": forked.session_id}, False)
                await router(
                    "session/resume",
                    {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []},
                    False,
                )
                assert "t3-code" not in str(
                    await native.data("GET", native.path(session_id, "/mcp"))
                )
                await router("session/close", {"sessionId": session_id}, False)
                assert (
                    await native.data("GET", native.path(session_id, "/client")) is None
                )
            finally:
                await agent.close()

    asyncio.run(scenario())
