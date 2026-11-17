"""HTTP client for the process-wide Yoke runtime used by the ACP adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from time import monotonic
from urllib.parse import quote

import httpx
from acp import RequestError

from yoke.acp.agent_monitor import AgentMonitor
from yoke.acp.observation.errors import NativeHttpError
from yoke.acp.observation.errors import native_request_failure
from yoke.acp.observation.hub import NativeEventHub
from yoke.acp.process_monitor import ProcessMonitor
from yoke.acp.prompting import InlineAttachment
from yoke.acp.state import ActiveTurn
from yoke.daemon.uds import async_unix_transport
from yoke.daemon.transport import Endpoint, TcpEndpoint

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


# Uvicorn closes idle keep-alive connections after 5 seconds. Expire pooled
# connections sooner so a request never reuses a socket the server is closing.
NATIVE_KEEPALIVE_EXPIRY_SECONDS = 2.0


def fail(message: str) -> RequestError:
    """Build a sanitized ACP server error."""
    return RequestError(-32001, message)


class NativeClient:
    """Small client for the native Yoke HTTP session API."""

    def __init__(self, url: str, token: str | None, *, uds: str | None = None) -> None:
        """Connect over TCP with a bearer token, or over the daemon socket *uds*.

        The daemon authorizes socket peers by uid, so *token* may be ``None``.
        """
        limits = httpx.Limits(keepalive_expiry=NATIVE_KEEPALIVE_EXPIRY_SECONDS)
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/v1/",
            headers={"Authorization": "Bearer " + token} if token else None,
            timeout=35,
            limits=limits,
            transport=(async_unix_transport(uds, limits=limits) if uds else None),
            trust_env=False,
            follow_redirects=False,
        )
        self.input_lifecycle = False
        self.process_monitor = ProcessMonitor(self)
        self.agent_monitor = AgentMonitor(self)
        self.event_hub = NativeEventHub(self)

    def use_daemon_endpoint(self, endpoint: Endpoint) -> None:
        """Refresh Windows requests and SSE after discovery rotates the token."""
        if isinstance(endpoint, TcpEndpoint):
            self.http.base_url = f"http://127.0.0.1:{endpoint.port}/api/v1/"
            self.http.headers["Authorization"] = f"Bearer {endpoint.token}"

    async def hold_lease(self) -> None:
        """Hold the contacted daemon alive, including for explicit HTTP URLs."""
        while True:
            try:
                async with self.http.stream(
                    "GET",
                    "daemon/lease",
                    params={"client": "acp", "pid": str(os.getpid())},
                    timeout=httpx.Timeout(10.0, read=None),
                ) as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if (
                            line.startswith("data: ")
                            and json.loads(line[6:]).get("reason") == "stop"
                        ):
                            return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.5)

    @staticmethod
    def path(session_id: str, suffix: str = "") -> str:
        return "session/" + quote(session_id, safe="") + suffix

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.agent_monitor.close()
        await self.process_monitor.close()
        await self.event_hub.close()
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Run one native request without exposing response bodies in failures."""
        started = monotonic()
        try:
            response = await self.http.request(method, path, **kwargs)
            if response.is_error:
                raise NativeHttpError(response.status_code)
            return response.json()
        except RequestError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise native_request_failure(
                exc, method=method, path=path, elapsed=monotonic() - started
            ) from None

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
        """Require logical idleness and drain; old daemons also need an empty queue."""
        status = await self.data("POST", self.path(session_id, "/wait?timeoutMs=1"))
        if status["state"] not in ("idle", "error"):
            raise fail("Native session already has active work")
        session = await self.data("GET", self.path(session_id))
        if not self.input_lifecycle and session["queue"]["total"]:
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
        answers: list[tuple[str, str]] = []
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
                for call in message.get("toolCalls", []):
                    await emit("session.tool.reconcile", call)
                if message.get("phase") != "commentary" and not message.get(
                    "toolCalls"
                ):
                    for block in message["content"]:
                        if block["type"] != "text":
                            raise fail(
                                "Native answer contains unsupported non-text content"
                            )
                        answers.append((message["id"], block["text"]))
        if not any(text.strip() for _, text in answers):
            raise fail("Native settlement has no persisted final answer")
        for message_id, text in answers:
            await emit("answer", {"messageID": message_id, "content": text})

    async def cancel_input(self, session_id: str, input_id: str) -> dict[str, Any]:
        """Cancel only this input; settlement wins a simultaneous cancel."""
        path = self.path(session_id, "/input/" + quote(input_id, safe=""))
        try:
            return await self.data("POST", path + "/cancel")
        except NativeHttpError as error:
            if error.status != 409:
                raise
            return await self.data("GET", path)

    async def turn(
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
        from yoke.acp.input_turn import InputTurn

        return await InputTurn(self).run(
            session_id,
            text,
            input_id,
            work,
            emit,
            attachments=attachments,
            continuation=continuation,
        )
