"""ACP message boundaries survive adjacent replies, settlement and reload."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from acp import schema as s

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient
from yoke.acp.prompting import replay_history
from yoke.acp.tools import ToolCallProjector
from yoke.acp.turns import emit


class Transcript:
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

    def messages(self) -> list[tuple[str, str, str]]:
        # ACP clients append same-role chunks until the message ID changes.
        result: list[tuple[str, str, str]] = []
        for update in self.updates:
            role = update["sessionUpdate"]
            message_id = update.get("messageId", "")
            text = update["content"].get("text", "<image>")
            if result and result[-1][:2] == (role, message_id):
                text = result.pop()[2] + text
            result.append((role, message_id, text))
        return result


def message(message_id: str, role: str, *texts: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": role,
        "content": [{"type": "text", "text": text} for text in texts],
        **extra,
    }


def test_replay_preserves_boundaries_and_native_ids_across_reloads() -> None:
    async def scenario() -> None:
        history = [
            message("user-1", "user", "Work for 8h"),
            message("assistant-1", "assistant", "Progress.", " More."),
            {"id": "control", "type": "control"},
            {"id": "tool", "type": "tool"},
            message("assistant-2", "assistant", "Progress."),
            message("assistant-3", "assistant", "Final."),
            message("user-2", "user", "Continue"),
            message("user-3", "user", "Correction"),
        ]
        first, restored = Transcript(), Transcript()
        await replay_history("session", history, first.update)
        await replay_history("session", history, restored.update)
        assert first.updates == restored.updates
        assert [(mid, text) for _, mid, text in first.messages()] == [
            ("user-1", "Work for 8h"),
            ("assistant-1", "Progress. More."),
            ("assistant-2", "Progress."),
            ("assistant-3", "Final."),
            ("user-2", "Continue"),
            ("user-3", "Correction"),
        ]

    asyncio.run(scenario())


def test_live_commentary_has_boundaries_without_intervening_tools() -> None:
    async def scenario() -> None:
        transcript = Transcript()
        agent = YokeAcpAgent(NativeClient("http://fixture", "unused"))
        agent.on_connect(SimpleNamespace(session_update=transcript.update))
        tools = ToolCallProjector()
        try:
            for input_id, iteration in [("first", 1), ("first", 2), ("steered", 1)]:
                await emit(
                    agent,
                    "session",
                    "session.message.updated",
                    {
                        "inputID": input_id,
                        "iteration": iteration,
                        "phase": "commentary",
                        "content": "Same progress text.",
                    },
                    tools,
                )
            assert len(transcript.messages()) == 3
            assert len({mid for _, mid, _ in transcript.messages()}) == 3
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_settled_answer_uses_the_same_ids_as_history_replay(monkeypatch) -> None:
    async def scenario() -> None:
        native = NativeClient("http://fixture", "unused")
        history = [
            message("user", "user", "Start", inputID="input"),
            message("commentary", "assistant", "Progress", phase="commentary"),
            message("answer-1", "assistant", "First.", " More."),
            message("answer-2", "assistant", "Second."),
            message("later", "user", "Later", inputID="later"),
            message("other-answer", "assistant", "Unrelated."),
        ]

        async def messages(session_id: str) -> list[dict[str, Any]]:
            return history

        monkeypatch.setattr(native, "messages", messages)
        live, restored = Transcript(), Transcript()
        agent = YokeAcpAgent(native)
        agent.on_connect(SimpleNamespace(session_update=live.update))
        tools = ToolCallProjector()

        async def forward(kind: str, data: dict[str, Any]) -> None:
            await emit(agent, "session", kind, data, tools)

        try:
            await native.final_answer("session", "input", forward)
            await replay_history("session", history, restored.update)
            assert [(mid, text) for _, mid, text in live.messages()] == [
                ("answer-1", "First. More."),
                ("answer-2", "Second."),
            ]
            assert all(row in restored.messages() for row in live.messages())
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_paged_long_history_keeps_every_message_after_reload() -> None:
    async def scenario() -> None:
        history = [message("user", "user", "Work for 8h")]
        history.extend(
            message(f"progress-{index}", "assistant", "Repeated progress.")
            for index in range(450)
        )
        history.append(message("final", "assistant", "Done."))
        pages: list[int] = []

        def respond(request: httpx.Request) -> httpx.Response:
            start = int(request.url.params.get("cursor", "0"))
            limit = int(request.url.params["limit"])
            assert request.url.params["order"] == "asc"
            pages.append(start)
            end = start + limit
            return httpx.Response(
                200,
                json={
                    "data": history[start:end],
                    "cursor": {"next": str(end) if end < len(history) else None},
                },
            )

        native = NativeClient("http://fixture", "unused")
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/", transport=httpx.MockTransport(respond)
        )
        try:
            transcript = Transcript()
            await replay_history(
                "session", await native.messages("session"), transcript.update
            )
            assert pages == [0, 200, 400]
            assert len(transcript.messages()) == len(history)
            assert [mid for _, mid, _ in transcript.messages()] == [
                row["id"] for row in history
            ]
        finally:
            await native.close()

    asyncio.run(scenario())
