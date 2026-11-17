"""One ACP prompt's native input ownership and settlement loop."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from yoke.acp.prompting import InlineAttachment
from yoke.acp.state import ActiveTurn
from yoke.acp.native import NativeStreamResync, fail

if TYPE_CHECKING:
    from yoke.acp.native import Emit, NativeClient


LOGGER = logging.getLogger(__name__)


class InputTurn:
    """Own one input through authoritative daemon phases."""

    def __init__(self, native: NativeClient) -> None:
        self.native = native

    async def run(  # noqa: C901 - lifecycle state machine kept linear for wire ordering
        self,
        session_id: str,
        text: str,
        input_id: str,
        work: ActiveTurn,
        emit: Emit,
        *,
        attachments: list[InlineAttachment] | None = None,
        continuation: bool = False,
    ) -> str:
        """Admit one prompt and bridge native lifecycle events until settlement."""
        work.input_id = input_id
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        ready = asyncio.Event()
        stream = asyncio.create_task(self.native.events(queue, ready))
        admitted = False
        finished = False
        after = 0
        outcome: str | None = None

        def observe(data: dict[str, Any]) -> None:
            nonlocal outcome
            revision = data.get("inputRevision")
            if work.phase in {"settled", "removed"} or (
                revision is not None and revision <= work.input_revision
            ):
                return
            if revision is not None:
                work.input_revision = revision
            previous = work.phase
            work.phase = data["phase"]
            LOGGER.debug(
                "machine=acp_input id=%s from=%s to=%s revision=%s",
                input_id,
                previous,
                work.phase,
                work.input_revision,
            )
            if work.phase == "settled":
                outcome = data["outcome"]
            elif work.phase == "removed":
                outcome = "stopped"
            elif work.phase == "paused":
                raise fail(
                    f"Native input {input_id} remains paused ({data.get('pauseCause') or 'user'})"
                )

        async def read_input() -> None:
            snapshot = await self.native.data(
                "GET", self.native.path(session_id, "/input"), params={"live": False}
            )
            record = next(
                (item for item in snapshot["inputs"] if item["id"] == input_id), None
            )
            if record is None:
                raise fail("Native admitted input is missing from its snapshot")
            observe({**record, "inputRevision": snapshot["inputRevision"]})

        async def event(item: dict[str, Any]) -> None:
            nonlocal outcome
            if item.get("sessionID") != session_id:
                return
            kind, data = item["type"], item["data"]
            if "permission" in kind or "question" in kind:
                return  # SessionNotifications owns bidirectional client requests.
            if data.get("inputID") != input_id:
                return
            if kind == "session.input.changed":
                observe(data)
            await emit(kind, data)

        async def check_stream() -> None:
            nonlocal stream, queue, ready
            recovered = False
            while stream.done():
                try:
                    await stream
                except NativeStreamResync:
                    # Reopen before history/input reads. Another overflow during
                    # catch-up repeats recovery without readmitting the prompt.
                    queue = asyncio.Queue(maxsize=1024)
                    ready = asyncio.Event()
                    stream = asyncio.create_task(self.native.events(queue, ready))
                    await asyncio.wait_for(ready.wait(), 30)
                    recovered = True
                else:
                    raise fail("Native event stream disconnected")
            if recovered and admitted:
                await read_input()

        async def receive() -> None:
            if stream.done():
                await check_stream()
            while not queue.empty():
                await event(queue.get_nowait())

        try:
            await asyncio.wait_for(ready.wait(), 30)
            if stream.done():
                await check_stream()
            while True:
                page = await self.native.request(
                    "GET",
                    self.native.path(session_id, "/history"),
                    params={"after": after, "limit": 200},
                )
                for item in page["data"]:
                    after = item["durable"]["seq"]
                    if item["data"].get("inputID") == input_id:
                        raise fail(
                            "Input ID already exists; resume native history instead of retrying the prompt"
                        )
                if not page["hasMore"]:
                    break
            # A valid ACP prompt has already been accepted by the client. Even
            # when cancellation wins during setup, admit its input before stop
            # checkpoints it; otherwise rapid steering silently loses messages.
            prompt: dict[str, Any] = {"text": text}
            if continuation:
                prompt["continuation"] = True
            if attachments:
                prompt["attachments"] = []
                for attachment in attachments:
                    uploaded = await self.native.data(
                        "POST",
                        "upload",
                        params={"sessionID": session_id},
                        files={
                            "file": (attachment.name, attachment.data, attachment.mime)
                        },
                    )
                    prompt["attachments"].append(
                        {
                            "type": "file",
                            "uri": uploaded["uri"],
                            "name": uploaded["name"],
                            "mime": uploaded["mime"],
                        }
                    )
            admitted = True
            receipt = await self.native.data(
                "POST",
                self.native.path(session_id, "/prompt"),
                json={"id": input_id, "prompt": prompt, "delivery": "queue"},
            )
            if receipt.get("id") != input_id or receipt["phase"] == "removed":
                raise fail("Native admission receipt is invalid or removed")
            after = max(after, receipt.get("admittedSeq", after))
            await read_input()
            while outcome is None:
                await receive()
                if outcome is not None:
                    break
                if work.cancel:
                    observe(await self.native.cancel_input(session_id, input_id))
                    work.wake.clear()
                    work.cancel = False
                    if outcome is not None:
                        break
                page = await self.native.request(
                    "GET",
                    self.native.path(session_id, "/history"),
                    params={"after": after, "limit": 200},
                )
                for item in page["data"]:
                    after = item["durable"]["seq"]
                    if item["data"].get("inputID") != input_id:
                        continue
                    if item["type"] == "session.input.changed":
                        observe(item["data"])
                if outcome is not None or page["hasMore"]:
                    continue
                incoming = asyncio.create_task(queue.get())
                stopped = asyncio.create_task(work.wake.wait())
                waited = asyncio.create_task(
                    self.native.data(
                        "POST", self.native.path(session_id, "/wait?timeoutMs=1000")
                    )
                )
                try:
                    done, _ = await asyncio.wait(
                        [incoming, stopped, waited, stream],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stream in done:
                        await check_stream()
                    if incoming in done:
                        await event(incoming.result())
                    if waited in done:
                        waited.result()
                        await read_input()
                finally:
                    for task in (incoming, stopped, waited):
                        task.cancel()
                    await asyncio.gather(
                        incoming, stopped, waited, return_exceptions=True
                    )
            if outcome == "stopped" and work.phase != "removed":
                await self.native.drained(session_id)
            finished = True
            await receive()
            if outcome == "stopped":
                return "cancelled"
            if outcome != "completed":
                raise fail("Native runtime failed")
            await self.native.final_answer(
                session_id, input_id, emit, continuation=continuation
            )
            return "end_turn"
        finally:

            async def release() -> None:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
                if admitted and not finished:
                    await self.native.cancel_input(session_id, input_id)

            cleanup = asyncio.create_task(release())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError
