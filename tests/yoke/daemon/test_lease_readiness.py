"""Reconnect readiness means the replacement accepted the lease stream."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from yoke.daemon.lease import hold_lease


def test_lease_callback_waits_for_accepted_stream(monkeypatch):
    import yoke.daemon.lease as module

    async def scenario():
        accepted = asyncio.Event()
        entered = asyncio.Event()
        callbacks = []

        class Stream:
            def raise_for_status(self):
                assert accepted.is_set()

            async def aiter_lines(self):
                yield 'data: {"leaseID": "accepted"}'
                yield 'data: {"reason": "stop"}'

        class Client:
            @asynccontextmanager
            async def stream(self, *args, **kwargs):
                entered.set()
                await accepted.wait()
                yield Stream()

        @asynccontextmanager
        async def client(*args, **kwargs):
            yield Client()

        monkeypatch.setattr(
            module, "connect_or_spawn", lambda paths: SimpleNamespace(socket="test")
        )
        monkeypatch.setattr(module, "async_client", client)
        task = asyncio.create_task(hold_lease("acp", on_connect=callbacks.append))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert callbacks == []
            accepted.set()
            await asyncio.wait_for(task, 1)
            assert len(callbacks) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
