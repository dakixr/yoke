"""Accepted ACP messages survive cancellation before native admission."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from yoke.acp.bridge import YokeAcpAgent
from tests.yoke.acp.test_native_input import Connection, ReceiptNative
from tests.yoke.acp.test_prompting import resource
from tests.yoke.http.test_attachment_continuation import RecordingProvider, app_for


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("stage", ["preflight", "upload"])
def test_early_cancel_preserves_input_for_the_next_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    indexed: bool,
    stage: str,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
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
            agent.on_connect(Connection())
            original_idle = native.assert_idle
            original_data = native.data

            async def held_idle(session_id: str) -> None:
                if stage == "preflight":
                    entered.set()
                    await release.wait()
                await original_idle(session_id)

            async def held_data(method: str, path: str, **kwargs):
                if stage == "upload" and path == "upload":
                    entered.set()
                    await release.wait()
                return await original_data(method, path, **kwargs)

            try:
                data = await native.data(
                    "POST",
                    "session",
                    json={
                        "id": "early-session",
                        "title": "Early cancellation",
                        "location": {"directory": str(tmp_path)},
                    },
                )
                agent.remember(
                    "early-session",
                    data,
                    {
                        "fixture:model": {
                            "id": "fixture:model",
                            "name": "Fixture",
                            "images": True,
                            "reasoningEfforts": [],
                            "defaultReasoningEffort": None,
                        }
                    },
                    "fixture:model",
                )
                first = [{"type": "text", "text": "first correction"}]
                if stage == "upload":
                    first.append(
                        resource(b"preserve these bytes", name="correction.txt")
                    )
                provider.release.clear()
                with monkeypatch.context() as patch:
                    patch.setattr(native, "assert_idle", held_idle)
                    patch.setattr(native, "data", held_data)
                    prompt = asyncio.create_task(
                        agent.prompt(
                            "early-session",
                            first,
                            yokeInputID="first",
                        )
                    )
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        cancelling = asyncio.create_task(agent.cancel("early-session"))
                        await asyncio.wait_for(
                            agent.active["early-session"]["wake"].wait(), 3
                        )
                        release.set()
                        # Admission and logical interruption must happen even
                        # though cancellation arrived before the native receipt.
                        await asyncio.wait_for(native.interrupted.wait(), 3)
                    finally:
                        release.set()
                        provider.release.set()
                        await asyncio.gather(prompt, return_exceptions=True)
                    await asyncio.wait_for(cancelling, 3)
                    assert (await prompt).stop_reason == "cancelled"
                assert "early-session" not in agent.active
                result = await agent.prompt(
                    "early-session",
                    [
                        {"type": "text", "text": "second correction"},
                    ],
                    yokeInputID="second",
                )
                assert result.stop_reason == "end_turn"
                users = [m for m in provider.calls[-1] if m.role == "user"]
                assert len(users) == 2
                assert "first correction" in (users[0].text_content() or "")
                assert users[1].plain_text_content == "second correction"
                if stage == "upload":
                    assert "correction.txt" in (users[0].text_content() or "")
                rows = await native.messages("early-session")
                assert [row["inputID"] for row in rows if row["type"] == "user"] == [
                    "first",
                    "second",
                ]
                assert (await native.data("GET", "session/early-session"))["queue"][
                    "total"
                ] == 0
            finally:
                release.set()
                provider.release.set()
                await agent.close()

    asyncio.run(scenario())
