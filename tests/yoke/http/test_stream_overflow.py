"""A bounded stream must report every gap before delivering later events."""

import asyncio

import pytest

from yoke.http.services.event_broker import GlobalEventBroker, live_event


@pytest.mark.parametrize("size", [1, 2, 512])
@pytest.mark.parametrize("ephemeral", [False, True])
def test_overflow_closes_with_resync_before_any_later_event(size, ephemeral):
    async def scenario():
        broker = GlobalEventBroker(queue_size=size)
        slow = broker.subscribe("slow")
        other = broker.subscribe("other")
        try:
            for index in range(size + 10):
                broker.publish(
                    live_event(
                        "session.tool.delta", {"index": index}, session_id="slow"
                    ),
                    ephemeral=ephemeral,
                )
            await asyncio.sleep(0)
            assert slow.closed
            assert slow.queue.qsize() <= size
            notice = slow.queue.get_nowait()
            assert notice is not None
            assert notice.type == "server.resyncRequired"
            assert notice.data == {"reason": "slow_consumer"}
            while not slow.queue.empty():
                assert slow.queue.get_nowait() is None
            assert not other.closed
            assert other.queue.empty()
            broker.publish(live_event("session.tool.ended", {}, session_id="slow"))
            await asyncio.sleep(0)
            assert slow.queue.empty()
        finally:
            broker.close()

    asyncio.run(scenario())
