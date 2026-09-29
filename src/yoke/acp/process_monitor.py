"""Read-only ACP observation of runtime-owned background processes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from yoke.acp.observation.events import Invalidations as _Invalidations

if TYPE_CHECKING:
    from yoke.acp.native import NativeClient

LOGGER = logging.getLogger(__name__)
Update = Callable[[str, dict[str, Any]], Awaitable[None]]


def _state(info: dict[str, Any]) -> str:
    code = info.get("exitCode")
    if info.get("status") == "running":
        return "running"
    if type(code) is int and code < 0:
        return "stopped"
    if info.get("status") == "failed" or (type(code) is int and code != 0):
        return "failed"
    if info.get("status") == "exited" and type(code) is int and code == 0:
        return "completed"
    return "unknown"


def _short(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 3] + "..."


class _Observed:
    def __init__(
        self,
        session_id: str,
        runtime_id: int,
        info: dict[str, Any],
        tool_call_id: str,
        update: Update,
    ) -> None:
        self.session_id = session_id
        self.runtime_id = runtime_id
        self.process_id: str = info["processID"]
        self.command: str = info["command"]
        self.tool_call_id = tool_call_id
        self.update = update
        self.sent: tuple[str, str, int | None] | None = None

    async def publish(self, info: dict[str, Any] | None) -> str:
        state = _state(info) if info is not None else "unknown"
        tail = info.get("output", {}).get("tail", "") if info else ""
        summary = tail if isinstance(tail, str) else ""
        if len(summary) > 4000:
            marker = "[Earlier output truncated]\n"
            summary = marker + summary[-(4000 - len(marker)) :]
        code = info.get("exitCode") if info else None
        code = code if type(code) is int else None
        signature = (state, summary, code)
        if self.sent == signature:
            return state
        reason = {
            "running": "Process is running.",
            "completed": "Process completed.",
            "failed": "Process failed.",
            "stopped": "Process stopped.",
            "unknown": "Monitoring disconnected; process status is unknown.",
        }[state]
        if state == "unknown" and info is not None:
            reason = "Process exit status is unknown."
        # Bound both raw summary and ACP text content independently.
        text = reason + ("\n" + summary if summary else "")
        elapsed = info.get("elapsedMs") if info else None
        raw: dict[str, Any] = {
            "type": "yoke_process",
            "processId": self.process_id,
            "status": state,
            "command": _short(self.command, 200),
            "exitCode": code,
            "elapsedMs": max(0, elapsed) if type(elapsed) is int else 0,
            "toolUseId": self.tool_call_id,
        }
        if summary:
            raw["summary"] = summary
        await self.update(
            self.session_id,
            {
                "sessionUpdate": "tool_call"
                if self.sent is None
                else "tool_call_update",
                "toolCallId": f"yoke-process:{self.process_id}",
                "kind": "other",
                "title": _short("Monitoring " + " ".join(self.command.split()), 200),
                "status": (
                    "in_progress"
                    if state == "running"
                    else "completed"
                    if state == "completed"
                    else "failed"
                ),
                "rawOutput": raw,
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": _short(text, 4000)},
                    }
                ],
            },
        )
        self.sent = signature
        return state


class ProcessMonitor:
    """Own one separate native event stream for all tracked process lifetimes."""

    def __init__(
        self,
        native: NativeClient,
        *,
        reconciliation_tick: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.native = native
        self._lock = asyncio.Lock()
        self._observed: dict[str, _Observed] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closed = False
        # Native process invalidations are ephemeral and may be dropped by a
        # busy broker. This only verifies state, never reruns or signals a job.
        self._reconciliation_tick = reconciliation_tick or self._tick

    @staticmethod
    async def _tick() -> None:
        await asyncio.sleep(15)

    async def track(
        self, session_id: str, runtime_id: int, tool_call_id: str, update: Update
    ) -> None:
        """Publish an initial snapshot before returning; never fail the real tool."""
        if type(runtime_id) is not int or runtime_id < 0 or not session_id:
            return
        try:
            async with self._lock:
                if self._closed:
                    return
                if self._watch_task is None or self._watch_task.done():
                    self._ready = asyncio.Event()
                    self._watch_task = asyncio.create_task(self._watch(self._ready))
                watch = self._watch_task
                await self._ready.wait()
                if self._closed or watch.done():
                    return
                items = await self.native.data(
                    "GET", "process", params={"sessionID": session_id, "limit": 200}
                )
                if not isinstance(items, list):
                    raise ValueError("Invalid process list")
                info = next(
                    (
                        item
                        for item in items
                        if isinstance(item, dict)
                        and item.get("sessionID") == session_id
                        and item.get("runtimeSessionID") == runtime_id
                    ),
                    None,
                )
                if info is None:
                    return
                process_id = info["processID"]
                if not isinstance(process_id, str) or not process_id:
                    raise ValueError("Invalid process ID")
                if process_id in self._observed:
                    return
                # A live invalidation can arrive while the list is in flight.
                # The observer holds its queue until this initial read is done.
                observed = _Observed(session_id, runtime_id, info, tool_call_id, update)
                try:
                    snapshot = await self._snapshot(session_id, runtime_id, process_id)
                except Exception:  # noqa: BLE001 - no trustworthy initial status
                    LOGGER.exception("Initial process snapshot failed")
                    await observed.publish(None)
                    return
                self._observed[process_id] = observed
                try:
                    state = await observed.publish(snapshot)
                except Exception:
                    self._observed.pop(process_id, None)
                    raise
                if state != "running":
                    self._observed.pop(process_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - observation cannot fail a completed tool
            LOGGER.exception("Failed to start process observation")
            async with self._lock:
                watch = self._watch_task if not self._observed else None
                if watch is not None:
                    watch.cancel()
            if watch is not None:
                await asyncio.gather(watch, return_exceptions=True)

    async def _snapshot(
        self, session_id: str, runtime_id: int, process_id: str
    ) -> dict[str, Any]:
        info = await self.native.data("GET", "process/" + quote(process_id, safe=""))
        if (
            not isinstance(info, dict)
            or info.get("processID") != process_id
            or info.get("sessionID") != session_id
            or info.get("runtimeSessionID") != runtime_id
        ):
            raise ValueError("Process ownership changed")
        return info

    async def _watch(self, ready: asyncio.Event) -> None:
        queue = _Invalidations()
        stream = asyncio.create_task(self.native.events(queue, ready))
        reconcile = asyncio.ensure_future(self._reconciliation_tick())
        try:
            await ready.wait()
            if stream.done():
                await stream
            while True:
                incoming = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait(
                        (incoming, stream, reconcile),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stream in done:
                        await stream
                        raise RuntimeError("Process event stream ended")
                    refresh_all = reconcile in done
                    if refresh_all:
                        await reconcile
                        reconcile = asyncio.ensure_future(self._reconciliation_tick())
                    item = incoming.result() if incoming in done else {}
                finally:
                    if not incoming.done():
                        incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
                if not refresh_all and item.get("type") != "session.process.updated":
                    continue
                async with self._lock:
                    for process_id, observed in list(self._observed.items()):
                        if not refresh_all and observed.session_id != item.get(
                            "sessionID"
                        ):
                            continue
                        info = await self._snapshot(
                            observed.session_id,
                            observed.runtime_id,
                            process_id,
                        )
                        if await observed.publish(info) != "running":
                            self._observed.pop(process_id, None)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - disconnected observation is not job failure
            LOGGER.exception("Process observation disconnected")
        finally:
            ready.set()
            stream.cancel()
            reconcile.cancel()
            await asyncio.gather(stream, reconcile, return_exceptions=True)
            async with self._lock:
                pending = list(self._observed.values())
                self._observed.clear()
                for observed in pending:
                    try:
                        await observed.publish(None)
                    except Exception:  # noqa: BLE001 - try every UI cleanup callback
                        LOGGER.exception("Failed to clear process observation")
                if self._watch_task is asyncio.current_task():
                    self._watch_task = None

    async def close(self) -> None:
        """End observation, not the underlying jobs."""
        self._closed = True
        watch = self._watch_task
        if watch is not None:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
