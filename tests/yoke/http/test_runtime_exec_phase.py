"""One execution owns its capacity and releases it exactly once."""

import asyncio

from tests.yoke.http.test_runtime_ownership import _execution


def test_release_before_acquisition_and_duplicate_cleanup_do_not_add_capacity():
    execution = _execution()
    slots = asyncio.Semaphore(1)
    execution.release_slot(slots)

    async def acquire():
        await slots.acquire()
        execution.wait_for_slot()
        execution.acquired_slot()

    asyncio.run(acquire())
    assert slots.locked()
    execution.release_slot(slots)
    execution.release_slot(slots)
    assert slots._value == 1
