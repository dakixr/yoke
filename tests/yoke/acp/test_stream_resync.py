"""An SSE gap must refresh observers without cancelling admitted work."""

import asyncio
import httpx
import pytest

from tests.yoke.acp.test_bridge import FixtureNative
from yoke.acp.input_turn import InputTurn
from yoke.acp.native import NativeClient, NativeStreamResync
from yoke.acp.observation.hub import NativeEventHub
from yoke.acp.state import ActiveTurn


def test_hub_refreshes_snapshots_immediately_after_resync():
    async def scenario():
        native = NativeClient("http://fixture", None)
        calls = 0
        gap = asyncio.Event()
        refreshed = asyncio.Event()
        received = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"server.connected"}\n\n'
                if calls == 1:
                    await gap.wait()
                    yield b'data: {"type":"server.resyncRequired","data":{"reason":"slow_consumer"}}\n\n'
                else:
                    await asyncio.Event().wait()

        def respond(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=Stream())

        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/", transport=httpx.MockTransport(respond)
        )
        hub = NativeEventHub(native)

        async def observe(event):
            received.append(event)
            if event is None and calls == 2:
                refreshed.set()

        try:
            await hub.subscribe(observe)
            await asyncio.wait_for(hub.ready.wait(), 1)
            gap.set()
            await asyncio.wait_for(refreshed.wait(), 3)
            assert {"type": "__disconnected__"} in received
            assert calls == 2
        finally:
            await hub.close()
            await native.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("gaps", [1, 2])
def test_prompt_resync_recovers_history_and_answer_without_readmission(
    gaps, monkeypatch
):
    async def scenario():
        class InputNative(FixtureNative):
            async def respond(self, request):
                if request.url.path.endswith("/input"):
                    self.calls.append((request.method, "session/native-id/input"))
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "inputRevision": 1 if self.hold else 2,
                                "inputs": [
                                    {
                                        "id": "input-1",
                                        "phase": "running" if self.hold else "settled",
                                        "outcome": None if self.hold else "completed",
                                    }
                                ],
                            }
                        },
                    )
                return await super().respond(request)

        native = InputNative()
        native.hold = True
        streams = 0

        async def events(queue, ready):
            nonlocal streams
            streams += 1
            native.queue = queue
            ready.set()
            if streams <= gaps:
                await native.admitted.wait()
                # Exercise the actual SSE decoder's resync failure.
                decoder = NativeClient("http://fixture", None)
                await decoder.http.aclose()
                decoder.http = httpx.AsyncClient(
                    base_url="http://fixture/api/v1/",
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200, text='data: {"type":"server.resyncRequired"}\n\n'
                        )
                    ),
                )
                try:
                    await decoder.events(queue, ready)
                finally:
                    await decoder.close()
            else:
                native.hold = native.running = False
                await asyncio.Event().wait()

        monkeypatch.setattr(native, "events", events)
        work = ActiveTurn(False, asyncio.Event(), asyncio.Event(), None)
        emitted = []

        async def emit(kind, data):
            emitted.append((kind, data))

        try:
            result = await asyncio.wait_for(
                InputTurn(native).run("native-id", "hello", "input-1", work, emit), 3
            )
            assert result == "end_turn"
            assert streams == gaps + 1
            assert not native.interrupted
            assert native.calls.count(("POST", "session/native-id/prompt")) == 1
            assert ("answer", {"messageID": "answer-id", "content": "Done."}) in emitted
            assert native.admission is not None
            assert native.admission["id"] == "input-1"
            assert work.phase == "settled"
            assert work.input_revision == 2
        finally:
            await native.close()

    asyncio.run(scenario())


def test_local_event_queue_overflow_also_requests_recovery():
    async def scenario():
        native = NativeClient("http://fixture", None)
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text='data: {"type":"session.tool.delta"}\n\ndata: {"type":"session.tool.ended"}\n\n',
                )
            ),
        )
        queue = asyncio.Queue(maxsize=1)
        try:
            with pytest.raises(NativeStreamResync):
                await native.events(queue, asyncio.Event())
            assert queue.qsize() == 1
        finally:
            await native.close()

    asyncio.run(scenario())
