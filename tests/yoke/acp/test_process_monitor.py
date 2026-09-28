"""Background process observation needs no running Yoke service or timer."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import httpx

from yoke.acp.native import NativeClient
from yoke.acp.process_monitor import ProcessMonitor


def process(
    session: str = "owner",
    runtime: int = 7,
    *,
    status: str = "running",
    code: int | None = None,
    tail: str = "",
    elapsed: int = 10,
) -> dict[str, Any]:
    return {
        "processID": f"proc_{session}_{runtime}",
        "sessionID": session,
        "runtimeSessionID": runtime,
        "command": "echo hi",
        "status": status,
        "exitCode": code,
        "elapsedMs": elapsed,
        "output": {"tail": tail},
    }


class MockNative(NativeClient):
    def __init__(self) -> None:
        super().__init__("http://fixture", "secret")
        self.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.MockTransport(self.respond),
        )
        self.items = [process(), process("other", 7)]
        self.calls: list[tuple[str, str]] = []
        self.events_queue: asyncio.Queue[dict[str, Any]] | None = None
        self.stream_started = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.fetch_started = asyncio.Event()
        self.fetch_release = asyncio.Event()
        self.fetch_release.set()
        self.snapshots: asyncio.Queue[None] = asyncio.Queue()
        self.snapshot_started = asyncio.Event()
        self.snapshot_release = asyncio.Event()
        self.snapshot_release.set()

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        self.calls.append((request.method, path))
        if request.method != "GET":
            raise AssertionError("observation must not control processes")
        if path == "process":
            assert request.url.params["sessionID"] == "owner"
            assert request.url.params["limit"] == "200"
            self.fetch_started.set()
            await self.fetch_release.wait()
            data = [item.copy() for item in self.items if item["sessionID"] == "owner"]
        elif path.startswith("process/"):
            data = next(
                item.copy()
                for item in self.items
                if item["processID"] == path.removeprefix("process/")
            )
            self.snapshot_started.set()
            await self.snapshot_release.wait()
            self.snapshots.put_nowait(None)
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"data": data})

    async def events(
        self, queue: asyncio.Queue[dict[str, Any]], ready: asyncio.Event
    ) -> None:
        self.events_queue = queue
        self.stream_started.set()
        ready.set()
        await self.disconnect.wait()
        raise RuntimeError("stream disconnected")

    def invalidate(self, session: str = "owner") -> None:
        assert self.events_queue is not None
        self.events_queue.put_nowait(
            {"type": "session.process.updated", "sessionID": session, "data": {}}
        )


class ProcessMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.native = MockNative()
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def asyncTearDown(self) -> None:
        self.native.fetch_release.set()
        self.native.snapshot_release.set()
        await self.native.close()

    async def update(self, session: str, payload: dict[str, Any]) -> None:
        self.assertEqual(session, "owner")
        self.messages.put_nowait(payload)

    async def next(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.messages.get(), 1)

    async def test_lost_exit_invalidation_is_reconciled_without_reexecuting(
        self,
    ) -> None:
        ticks: asyncio.Queue[None] = asyncio.Queue()

        async def tick() -> None:
            await ticks.get()

        self.native.process_monitor = ProcessMonitor(
            self.native, reconciliation_tick=tick
        )
        await self.native.process_monitor.track("owner", 7, "call", self.update)
        await self.next()
        self.native.items[0] = process(status="exited", code=0, tail="done")
        # No SSE invalidation, as happens when the native broker drops it.
        ticks.put_nowait(None)
        terminal = await self.next()
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["rawOutput"]["summary"], "done")
        self.assertTrue(all(method == "GET" for method, _ in self.native.calls))

    async def test_turn_can_end_before_process_does(self) -> None:
        await self.native.process_monitor.track("owner", 7, "original", self.update)
        start = await self.next()
        self.assertEqual(start["sessionUpdate"], "tool_call")
        self.assertEqual(start["toolCallId"], "yoke-process:proc_owner_7")
        self.assertEqual(start["status"], "in_progress")
        self.assertEqual(start["rawOutput"]["toolUseId"], "original")
        self.assertEqual(start["rawOutput"]["status"], "running")
        self.assertEqual(start["kind"], "other")
        self.native.items[0] = process(status="exited", code=0, tail="done")
        self.native.invalidate()
        end = await self.next()
        self.assertEqual(end["sessionUpdate"], "tool_call_update")
        self.assertEqual(end["status"], "completed")
        self.assertEqual(end["rawOutput"]["summary"], "done")
        await self.native.close()
        self.assertTrue(self.messages.empty())
        self.assertTrue(all(method == "GET" for method, _ in self.native.calls))

    async def test_other_sessions_and_elapsed_only_are_ignored(self) -> None:
        await self.native.process_monitor.track("owner", 7, "original", self.update)
        await self.next()
        await self.native.snapshots.get()
        self.native.invalidate("other")
        self.native.items[0] = process(elapsed=2000)
        self.native.invalidate()
        await self.native.snapshots.get()
        async with self.native.process_monitor._lock:
            self.assertTrue(self.messages.empty())
        self.native.items[0] = process(tail="stdout", elapsed=3000)
        self.native.invalidate()
        changed = await self.next()
        self.assertEqual(changed["rawOutput"]["summary"], "stdout")
        await self.native.snapshots.get()
        self.native.invalidate()
        await self.native.snapshots.get()
        async with self.native.process_monitor._lock:
            self.assertTrue(self.messages.empty())
        self.native.items[0] = process(tail="finished", elapsed=5000)
        self.native.invalidate()
        changed = await self.next()
        self.assertEqual(changed["rawOutput"]["summary"], "finished")
        self.assertTrue(self.messages.empty())

    async def test_invalidation_while_initial_fetch_is_in_flight(self) -> None:
        self.native.snapshot_release.clear()
        tracking = asyncio.create_task(
            self.native.process_monitor.track("owner", 7, "call", self.update)
        )
        await self.native.snapshot_started.wait()
        await self.native.stream_started.wait()
        self.native.items[0] = process(status="exited", code=0)
        self.native.invalidate()
        self.native.snapshot_release.set()
        await tracking
        self.assertEqual((await self.next())["status"], "in_progress")
        self.assertEqual((await self.next())["status"], "completed")

    async def test_nonzero_signal_and_unknown_exit(self) -> None:
        for code, status, expected in [
            (3, "exited", "failed"),
            (-15, "exited", "stopped"),
            (None, "exited", "unknown"),
            (None, "failed", "failed"),
        ]:
            await self.native.close()
            self.native = MockNative()
            self.native.items[0] = process(status=status, code=code)
            await self.native.process_monitor.track("owner", 7, "call", self.update)
            message = await self.next()
            self.assertEqual(message["rawOutput"]["status"], expected)
            self.assertEqual(message["status"], "failed")

    async def test_disconnection_and_close_do_not_complete_jobs(self) -> None:
        await self.native.process_monitor.track("owner", 7, "call", self.update)
        await self.next()
        self.native.disconnect.set()
        unknown = await self.next()
        self.assertEqual(unknown["rawOutput"]["status"], "unknown")
        self.assertIn(
            "Monitoring disconnected", unknown["content"][0]["content"]["text"]
        )
        # A later tool can open another observer after a failed stream.
        self.native.disconnect = asyncio.Event()
        await self.native.process_monitor.track("owner", 7, "call2", self.update)
        self.assertEqual((await self.next())["status"], "in_progress")
        await self.native.close()
        self.assertEqual((await self.next())["rawOutput"]["status"], "unknown")
        self.assertTrue(all(method == "GET" for method, _ in self.native.calls))

    async def test_callback_failure_does_not_escape_or_leave_a_running_job(
        self,
    ) -> None:
        async def broken(_session: str, _message: dict[str, Any]) -> None:
            raise RuntimeError("connection closed")

        await self.native.process_monitor.track("owner", 7, "call", broken)
        self.assertFalse(self.native.process_monitor._observed)
        await self.native.close()
        self.assertIsNone(self.native.process_monitor._watch_task)

    async def test_update_callback_failure_retires_observer(self) -> None:
        received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def broken_later(_session: str, message: dict[str, Any]) -> None:
            received.put_nowait(message)
            if message["sessionUpdate"] == "tool_call_update":
                raise RuntimeError("connection closed")

        await self.native.process_monitor.track("owner", 7, "call", broken_later)
        await received.get()
        self.native.items[0] = process(tail="new output")
        self.native.invalidate()
        self.assertEqual(
            (await asyncio.wait_for(received.get(), 1))["status"], "in_progress"
        )
        self.assertEqual(
            (await asyncio.wait_for(received.get(), 1))["rawOutput"]["status"],
            "unknown",
        )
        await self.native.close()
        self.assertIsNone(self.native.process_monitor._watch_task)

    async def test_output_is_bounded(self) -> None:
        self.native.items[0] = process(tail="x" * 9000 + "final error detail")
        self.native.items[0]["command"] = "echo " + "a" * 500
        await self.native.process_monitor.track("owner", 7, "call", self.update)
        message = await self.next()
        self.assertLessEqual(len(message["title"]), 200)
        self.assertLessEqual(len(message["rawOutput"]["summary"]), 4000)
        self.assertLessEqual(len(message["content"][0]["content"]["text"]), 4000)
        self.assertTrue(message["rawOutput"]["summary"].endswith("final error detail"))

    async def test_chat_traffic_and_repeated_invalidations_do_not_overflow(
        self,
    ) -> None:
        await self.native.process_monitor.track("owner", 7, "call", self.update)
        await self.next()
        assert self.native.events_queue is not None
        for _ in range(1000):
            self.native.events_queue.put_nowait(
                {
                    "type": "session.message.updated",
                    "sessionID": "owner",
                    "data": {},
                }
            )
            self.native.invalidate()
        self.assertEqual(self.native.events_queue.qsize(), 1)
        self.native.items[0] = process(status="exited", code=0)
        self.assertEqual((await self.next())["status"], "completed")
