"""HTTP client for the process-wide Yoke runtime used by the ACP adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from acp import RequestError

from yoke.acp.prompting import InlineAttachment

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


def fail(message: str) -> RequestError:
    """Build a sanitized ACP server error."""
    return RequestError(-32001, message)


class NativeClient:
    """Small client for the native Yoke HTTP session API."""

    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/v1/",
            headers={"Authorization": "Bearer " + token},
            timeout=35,
            trust_env=False,
            follow_redirects=False,
        )

    @staticmethod
    def path(session_id: str, suffix: str = "") -> str:
        return "session/" + quote(session_id, safe="") + suffix

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Run one native request without exposing response bodies in failures."""
        try:
            response = await self.http.request(method, path, **kwargs)
            if response.is_error:
                raise fail(f"Native HTTP error {response.status_code}")
            return response.json()
        except RequestError:
            raise
        except (httpx.HTTPError, ValueError):
            raise fail("Native HTTP transport or JSON failure") from None

    async def data(self, method: str, path: str, **kwargs: Any) -> Any:
        """Return the standard native response data member."""
        result = await self.request(method, path, **kwargs)
        if not isinstance(result, dict) or "data" not in result:
            raise fail("Native response is missing data")
        return result["data"]

    async def drained(self, session_id: str, timeout_ms: int = 30_000) -> None:
        """Require physical worker and owned-cleanup completion."""
        status = await self.data(
            "POST",
            self.path(session_id, f"/drain?timeoutMs={timeout_ms}"),
        )
        if status.get("drained") is not True or status.get("activeWorkers") != 0:
            raise fail("Native workers have not drained; completion is unconfirmed")

    async def assert_idle(self, session_id: str) -> None:
        """Require logical idleness, an empty queue, and physical drain."""
        status = await self.data("POST", self.path(session_id, "/wait?timeoutMs=1"))
        if status["state"] not in ("idle", "error"):
            raise fail("Native session already has active work")
        session = await self.data("GET", self.path(session_id))
        if session["queue"]["total"]:
            raise fail("Native session has queued inputs")
        await self.drained(session_id, 1)

    async def skills(self, directory: str) -> list[dict[str, Any]]:
        """Read the workspace skill catalog through the native authority."""
        result = await self.request(
            "GET",
            "skill",
            params={"directory": directory},
        )
        items = result.get("data") if isinstance(result, dict) else None
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise fail("Native skill catalog is invalid")
        return items

    async def stop(self, session_id: str) -> None:
        """Interrupt and wait for physical completion."""
        await self.data("POST", self.path(session_id, "/interrupt"))
        await self.drained(session_id)

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        """Read all persisted native messages in chronological order."""
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"order": "asc", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = await self.request(
                "GET", self.path(session_id, "/message"), params=params
            )
            result.extend(page["data"])
            cursor = page["cursor"]["next"]
            if not cursor:
                return result

    async def events(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        ready: asyncio.Event,
    ) -> None:
        """Copy the native SSE stream into a bounded local queue."""
        try:
            async with self.http.stream(
                "GET",
                "event",
                timeout=httpx.Timeout(35, read=None),
            ) as response:
                if response.status_code != 200:
                    raise fail(f"Native event HTTP error {response.status_code}")
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data.clear()
                        if event["type"] == "server.connected":
                            ready.set()
                        elif event["type"] == "server.resyncRequired":
                            raise fail("Native event stream requires resync")
                        else:
                            queue.put_nowait(event)
            raise fail("Native event stream disconnected")
        except asyncio.CancelledError:
            raise
        except RequestError:
            raise
        except Exception:  # noqa: BLE001 - sanitize untrusted SSE payload failures
            raise fail(
                "Native event stream disconnected, invalid, or overflowed"
            ) from None
        finally:
            ready.set()

    async def final_answer(
        self, session_id: str, input_id: str, emit: Emit, *, continuation: bool = False
    ) -> None:
        """Emit the persisted final assistant text for one admitted input."""
        found = False
        answers: list[str] = []
        for message in await self.messages(session_id):
            if continuation:
                found = message.get("inputID") == input_id
            if message["type"] == "user":
                if found:
                    break
                found = message.get("inputID") == input_id
            elif found and message["type"] == "assistant":
                if message.get("inputID") not in (None, input_id):
                    break  # A later promptless turn is not this prompt's answer.
                if message.get("phase") != "commentary" and not message.get(
                    "toolCalls"
                ):
                    for block in message["content"]:
                        if block["type"] != "text":
                            raise fail(
                                "Native answer contains unsupported non-text content"
                            )
                        answers.append(block["text"])
        if not any(text.strip() for text in answers):
            raise fail("Native settlement has no persisted final answer")
        for text in answers:
            await emit("answer", {"content": text})

    async def turn(
        self,
        session_id: str,
        text: str,
        input_id: str,
        work: dict[str, Any],
        emit: Emit,
        *,
        attachments: list[InlineAttachment] | None = None,
        continuation: bool = False,
    ) -> str:
        """Admit one prompt and bridge native lifecycle events until settlement."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        ready = asyncio.Event()
        stream = asyncio.create_task(self.events(queue, ready))
        admitted = False
        finished = False
        after = 0
        outcome: str | None = None
        idle_without_receipt = False

        async def event(item: dict[str, Any]) -> None:
            nonlocal outcome
            if item.get("sessionID") != session_id:
                return
            kind, data = item["type"], item["data"]
            if "permission" in kind or "question" in kind:
                raise fail("Native interactive permissions/questions are unsupported")
            if data.get("inputID") != input_id:
                return
            if kind == "session.runtime.failed":
                raise fail("Native runtime failed")
            if kind == "session.prompt.settled":
                outcome = data["outcome"]
            await emit(kind, data)

        async def receive() -> None:
            if stream.done():
                await stream
            while not queue.empty():
                await event(queue.get_nowait())

        try:
            await asyncio.wait_for(ready.wait(), 30)
            if stream.done():
                await stream
            await self.assert_idle(session_id)
            while True:
                page = await self.request(
                    "GET",
                    self.path(session_id, "/history"),
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
            if work["cancel"]:
                return "cancelled"
            prompt: dict[str, Any] = {"text": text}
            if continuation:
                prompt["continuation"] = True
            if attachments:
                prompt["attachments"] = []
                for attachment in attachments:
                    uploaded = await self.data(
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
            if work["cancel"]:
                return "cancelled"
            admitted = True
            receipt = await self.data(
                "POST",
                self.path(session_id, "/prompt"),
                json={"id": input_id, "prompt": prompt, "delivery": "queue"},
            )
            if receipt.get("id") != input_id or receipt.get("state") == "removed":
                raise fail("Native admission receipt is invalid or removed")
            after = max(after, receipt.get("admittedSeq", after))
            while outcome is None:
                if work["cancel"]:
                    await self.stop(session_id)
                    finished = True
                    return "cancelled"
                await receive()
                page = await self.request(
                    "GET",
                    self.path(session_id, "/history"),
                    params={"after": after, "limit": 200},
                )
                for item in page["data"]:
                    after = item["durable"]["seq"]
                    if item["data"].get("inputID") != input_id:
                        continue
                    if item["type"] == "session.runtime.failed":
                        raise fail("Native runtime failed")
                    if item["type"] == "session.prompt.settled":
                        outcome = item["data"]["outcome"]
                if outcome is not None or page["hasMore"]:
                    continue
                if idle_without_receipt:
                    raise fail("Native workers ended without a settlement receipt")
                incoming = asyncio.create_task(queue.get())
                stopped = asyncio.create_task(work["wake"].wait())
                waited = asyncio.create_task(
                    self.data("POST", self.path(session_id, "/wait?timeoutMs=1000"))
                )
                try:
                    done, _ = await asyncio.wait(
                        [incoming, stopped, waited, stream],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stream in done:
                        await stream
                    if incoming in done:
                        await event(incoming.result())
                    if waited in done:
                        state = waited.result()["state"]
                        if state == "waiting_input":
                            raise fail("Native interactive input is unsupported")
                        if state in ("idle", "error"):
                            await self.drained(session_id)
                            idle_without_receipt = True
                finally:
                    for task in (incoming, stopped, waited):
                        task.cancel()
                    await asyncio.gather(
                        incoming, stopped, waited, return_exceptions=True
                    )
            await self.drained(session_id)
            finished = True
            await receive()
            if outcome == "stopped" or work["cancel"]:
                return "cancelled"
            if outcome != "completed":
                raise fail("Native runtime failed")
            await self.final_answer(
                session_id, input_id, emit, continuation=continuation
            )
            return "end_turn"
        finally:

            async def release() -> None:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
                if admitted and not finished:
                    await self.stop(session_id)

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
