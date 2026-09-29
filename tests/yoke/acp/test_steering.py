"""T3's cancel/drain/re-prompt sequence through ACP and the native agent loop."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

from yoke.acp.bridge import YokeAcpAgent
from tests.yoke.acp.test_native_input import Connection, ReceiptNative
from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for


@pytest.mark.parametrize("indexed", [False, True])
def test_steering_preserves_native_history_after_physical_drain(
    tmp_path, monkeypatch, indexed: bool
) -> None:
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

    monkeypatch.setattr("yoke.acp.bridge.discover", catalog)

    async def scenario() -> None:
        provider = RecordingProvider()
        app = app_for(tmp_path, provider, indexed=indexed)
        async with app.router.lifespan_context(app):
            native = ReceiptNative("http://fixture", "unused")
            await native.http.aclose()
            native.http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture/api/v1/",
                headers={"Authorization": "Bearer runtime-lifetime-secret"},
            )
            agent = YokeAcpAgent(native)
            router = build_agent_router(agent, use_unstable_protocol=True)
            connection = Connection()
            agent.on_connect(connection)
            try:
                data = await native.data(
                    "POST",
                    "session",
                    json={
                        "id": "steer-session",
                        "title": "Steering regression",
                        "location": {"directory": str(tmp_path)},
                    },
                )
                models, default = await catalog()
                agent.remember("steer-session", data, models, default)

                async def prompt(text: str, input_id: str):
                    return await router(
                        "session/prompt",
                        {
                            "sessionId": "steer-session",
                            "prompt": [{"type": "text", "text": text}],
                            "_meta": {"yokeInputID": input_id},
                        },
                        False,
                    )

                original = "Keep the existing design and fix the active task."
                correction = "Use the smaller implementation instead."
                provider.release.clear()
                running = asyncio.create_task(prompt(original, "original-input"))
                try:
                    assert await asyncio.to_thread(provider.started.wait, 3)
                    cancellation = asyncio.create_task(
                        router("session/cancel", {"sessionId": "steer-session"}, True)
                    )
                    await asyncio.wait_for(native.interrupted.wait(), 3)
                    assert not cancellation.done()
                    assert not running.done()
                    assert len(provider.calls) == 1
                finally:
                    provider.release.set()
                await asyncio.wait_for(cancellation, 3)
                assert (await running).stop_reason == "cancelled"
                assert "steer-session" not in agent.busy
                assert "steer-session" not in agent.active
                assert not connection.updates

                result = await prompt(correction, "correction-input")
                assert result.stop_reason == "end_turn"
                assert len(provider.calls) == 2
                assert [
                    message.content
                    for message in provider.calls[1]
                    if message.role == "user"
                ] == [original, correction]
                rows = await native.messages("steer-session")
                assert [row["inputID"] for row in rows if row["type"] == "user"] == [
                    "original-input",
                    "correction-input",
                ]
                assert connection.updates == [
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer-2"},
                    }
                ]
                with pytest.raises(RequestError, match="already exists"):
                    await prompt(correction, "original-input")
                assert len(provider.calls) == 2

                restored = YokeAcpAgent(native)
                restored.on_connect(connection)
                await restored.resume_session("steer-session", str(tmp_path))
                result = await restored.prompt(
                    "steer-session",
                    [{"type": "text", "text": "Finish after resuming."}],
                    yokeInputID="resumed-input",
                )
                assert result.stop_reason == "end_turn"
                assert [
                    message.content
                    for message in provider.calls[2]
                    if message.role == "user"
                ] == [original, correction, "Finish after resuming."]
            finally:
                provider.release.set()
                await agent.close()

    asyncio.run(scenario())
