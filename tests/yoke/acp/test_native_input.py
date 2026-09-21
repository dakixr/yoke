"""ACP to in-process native HTTP integration; no servers or model calls."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from acp import RequestError
from acp import schema as s
from acp.agent.router import build_agent_router

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient
from tests.yoke.acp.test_prompting import resource
from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for, png


class ReceiptNative(NativeClient):
    def __init__(self, url: str, token: str) -> None:
        super().__init__(url, token)
        self.interrupted = asyncio.Event()

    async def stop(self, session_id: str) -> None:
        await self.data("POST", self.path(session_id, "/interrupt"))
        self.interrupted.set()
        await self.drained(session_id)

    async def events(self, queue, ready):
        # Exercise the durable receipt/wait path without an infinite SSE response
        # buffered by ASGITransport. SSE translation is covered in test_bridge.
        ready.set()
        await asyncio.Event().wait()


class Connection:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def session_update(self, session_id, update):
        s.SessionNotification.model_validate(
            {"sessionId": session_id, "update": update}
        )
        self.updates.append(update)


def test_acp_uploads_continues_and_loads_real_native_history(
    tmp_path, monkeypatch
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
        app = app_for(tmp_path, provider)
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
                initialized = (await agent.initialize(1)).model_dump(by_alias=True)
                assert initialized["agentCapabilities"]["promptCapabilities"]["image"]
                assert initialized["agentCapabilities"]["promptCapabilities"][
                    "embeddedContext"
                ]
                data = await native.data(
                    "POST",
                    "session",
                    json={
                        "id": "session-a",
                        "title": "Named",
                        "location": {"directory": str(tmp_path)},
                    },
                )
                models, default = await catalog()
                agent.remember("session-a", data, models, default)
                with pytest.raises(RequestError):
                    await agent.prompt(
                        "session-a", [], _meta={"yokeContinuation": True}
                    )
                image = {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": base64.b64encode(png()).decode(),
                    "_meta": {"yokeAttachmentName": "original.png"},
                }
                # All blocks validate before even the first upload.
                with pytest.raises(RequestError):
                    await agent.prompt("session-a", [image, {"type": "audio"}])
                assert not (tmp_path / "sessions" / "uploads").exists()
                assert (
                    await router(
                        "session/prompt",
                        {
                            "sessionId": "session-a",
                            "prompt": [image, resource(b"bytes only", name="file.bin")],
                            "_meta": {"yokeInputID": "first"},
                        },
                        False,
                    )
                ).stop_reason == "end_turn"
                prior = await native.messages("session-a")
                assert prior[0]["inputID"] == "first"
                assert len(provider.calls) == 1
                connection.updates.clear()
                assert (
                    await router(
                        "session/prompt",
                        {
                            "sessionId": "session-a",
                            "prompt": [],
                            "_meta": {
                                "yokeContinuation": True,
                                "yokeInputID": "continuation",
                            },
                        },
                        False,
                    )
                ).stop_reason == "end_turn"
                assert connection.updates == [
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer-2"},
                    }
                ]
                rows = await native.messages("session-a")
                assert rows[: len(prior)] == prior
                assert len([row for row in rows if row["type"] == "user"]) == 1
                assert rows[-1]["inputID"] == "continuation"
                with pytest.raises(RequestError, match="already exists"):
                    await agent.prompt(
                        "session-a",
                        [],
                        _meta={"yokeContinuation": True, "yokeInputID": "continuation"},
                    )
                assert len(provider.calls) == 2
                provider.started.clear()
                provider.release.clear()
                running = asyncio.create_task(
                    agent.prompt(
                        "session-a",
                        [],
                        _meta={"yokeContinuation": True, "yokeInputID": "cancelled"},
                    )
                )
                try:
                    assert await asyncio.to_thread(provider.started.wait, 3)
                    cancelling = asyncio.create_task(agent.cancel("session-a"))
                    await asyncio.wait_for(native.interrupted.wait(), 3)
                    assert not cancelling.done()
                finally:
                    provider.release.set()
                await asyncio.wait_for(cancelling, 3)
                assert (await running).stop_reason == "cancelled"
                assert (await native.messages("session-a"))[: len(prior)] == prior
                restored = YokeAcpAgent(native)
                restored.on_connect(connection)
                connection.updates.clear()
                await restored.load_session(str(tmp_path), "session-a")
                content = [update["content"] for update in connection.updates]
                replayed_image = next(
                    block for block in content if block["type"] == "image"
                )
                assert base64.b64decode(replayed_image["data"]) == png()
                assert replayed_image["_meta"]["yokeAttachmentName"] == "original.png"
                await restored.resume_session("session-a", str(tmp_path))
                connection.updates.clear()
                assert (
                    await restored.prompt(
                        "session-a",
                        [],
                        _meta={"yokeContinuation": True, "yokeInputID": "after-cancel"},
                    )
                ).stop_reason == "end_turn"
                assert connection.updates[-1]["content"]["text"] == "answer-4"
                with pytest.raises(RequestError, match="full-access"):
                    await restored.set_session_mode("session-a", "plan")
            finally:
                await agent.close()

    asyncio.run(scenario())
