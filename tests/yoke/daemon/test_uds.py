"""The Unix-socket client loop hides only anyio's cancelled-read wakeup race."""

from __future__ import annotations

import asyncio

from yoke.daemon.uds import ignore_cancelled_socket_wakeups


def _run_with_handler(schedule) -> list[dict]:
    reported: list[dict] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        ignore_cancelled_socket_wakeups(loop)
        installed = loop.get_exception_handler()
        assert installed is not None

        class RecordingLoop(asyncio.SelectorEventLoop):
            # The installed handler delegates through default_exception_handler.
            def default_exception_handler(self, context: dict) -> None:
                reported.append(context)

        recorder = RecordingLoop()
        loop.set_exception_handler(lambda _loop, context: installed(recorder, context))
        try:
            schedule(loop)
            for _ in range(3):
                await asyncio.sleep(0)
        finally:
            recorder.close()

    asyncio.run(scenario())
    return reported


def test_wakeup_of_a_cancelled_read_is_not_reported():
    def schedule(loop: asyncio.AbstractEventLoop) -> None:
        # The exact shape anyio produces: a cancelled wait future whose reader
        # callback still fires in the same iteration.
        waiter: asyncio.Future[None] = loop.create_future()
        waiter.cancel()
        loop.call_soon(waiter.set_result, None)

    assert _run_with_handler(schedule) == []


def test_other_callback_errors_still_reach_the_default_handler():
    def schedule(loop: asyncio.AbstractEventLoop) -> None:
        done: asyncio.Future[None] = loop.create_future()
        done.set_result(None)
        # Completed, not cancelled: a genuine double completion stays visible.
        loop.call_soon(done.set_result, None)

        def fail() -> None:
            raise RuntimeError("boom")

        loop.call_soon(fail)

    reported = _run_with_handler(schedule)
    assert [type(context["exception"]) for context in reported] == [
        asyncio.InvalidStateError,
        RuntimeError,
    ]
