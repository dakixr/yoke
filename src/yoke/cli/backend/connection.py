"""HTTP and reconnecting session events for a terminal attached to the daemon."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import httpx

from yoke.cli.backend.events import EventSubscriptions, event_from_sse
from yoke.daemon.client import DaemonHandle, connect_or_spawn, unix_client


class DaemonRequestError(ValueError):
    """A structured API rejection, including revision conflicts."""

    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.body = response.json()
        error = self.body.get("error", {})
        super().__init__(error.get("message", response.text))


class SessionConnection:
    """Own one attachment, its event lease, and durable recovery cursor.

    Mutations are never retried implicitly. Callers may retry prompt admission
    with the same input ID. Each reconnect opens the live subscription before
    reading history, so events published during recovery cannot fall in a gap.
    """

    def __init__(
        self,
        session_id: str,
        *,
        discover: Callable[[], DaemonHandle] = connect_or_spawn,
        cwd: Path | None = None,
        warning: Callable[[str], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.owner = f"cli-{uuid4().hex}"
        self.events = EventSubscriptions()
        self._discover = discover
        self._cwd = (cwd or Path.cwd()).resolve()
        self._warning = warning
        self._handle = discover()
        self._warned: set[str] = set()
        self._report_warning()
        self._seq = 0
        self._ready = Event()
        self._closed = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._worker: Thread | None = None

    def client_config(self) -> dict[str, Any]:
        """Take a fresh private environment snapshot, including after restart."""
        return {
            "owner": self.owner,
            "kind": "cli",
            "mode": "full-access",
            "env": dict(os.environ),
            "cwd": str(self._cwd),
        }

    def request(self, method: str, route: str, **kwargs: Any) -> dict[str, Any]:
        """Perform one request. In particular, do not replay ambiguous writes."""
        with unix_client(self._handle.socket, timeout=310) as client:
            response = client.request(method, route, **kwargs)
        if response.is_error:
            raise DaemonRequestError(response)
        return response.json()

    def session_request(
        self, method: str, suffix: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        return self.request(method, f"session/{self.session_id}{suffix}", **kwargs)

    def start(self, *, timeout: float = 10) -> None:
        """Attach and wait until the stream owns its lease and recovery is done."""
        if self._worker is None:
            self._worker = Thread(target=self._run, daemon=True, name="yoke-cli-events")
            self._worker.start()
        if not self._ready.wait(timeout):
            self.close()
            raise TimeoutError("Could not attach to Yoke daemon events.")

    def close(self) -> None:
        """Release only this client's attachment and stream lease."""
        self._closed.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)
        if self._worker is not None:
            self._worker.join(timeout=10)

    def _report_warning(self) -> None:
        warning = self._handle.warning
        if warning and warning not in self._warned:
            self._warned.add(warning)
            if self._warning:
                self._warning(warning)

    def _run(self) -> None:
        asyncio.run(self._listen())

    async def _listen(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            while not self._closed.is_set():
                try:
                    discovery = asyncio.create_task(asyncio.to_thread(self._discover))
                    try:
                        self._handle = await asyncio.shield(discovery)
                    except asyncio.CancelledError:
                        # A cancelled to_thread keeps running. Join discovery so
                        # close cannot race a late spawn against test cleanup.
                        self._handle = await discovery
                        raise
                    self._report_warning()
                    transport = httpx.AsyncHTTPTransport(uds=str(self._handle.socket))
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://yoke/api/v1/",
                        timeout=httpx.Timeout(10, read=None),
                        trust_env=False,
                    ) as http:
                        await self._attached_stream(http)
                except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                    if self._ready.is_set():
                        self.events.publish(
                            "client.disconnected", {"message": str(exc)}
                        )
                if not self._closed.is_set():
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            # Do not discover or spawn a daemon just to detach on exit.
            try:
                await asyncio.to_thread(
                    self.session_request,
                    "DELETE",
                    "/client",
                    params={"owner": self.owner},
                )
            except (httpx.HTTPError, OSError, ValueError):
                pass
            self.events.close()

    async def _attached_stream(self, http: httpx.AsyncClient) -> None:
        response = await http.put(
            f"session/{self.session_id}/client", json=self.client_config()
        )
        response.raise_for_status()
        async with http.stream(
            "GET", "event", params={"sessionID": self.session_id, "client": self.owner}
        ) as stream:
            stream.raise_for_status()
            data: list[str] = []
            async for line in stream.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif not line and data:
                    envelope = json.loads("\n".join(data))
                    data.clear()
                    if envelope["type"] in {
                        "server.connected",
                        "server.resyncRequired",
                    }:
                        await self._resync(http)
                        self._ready.set()
                    else:
                        self._publish(envelope)

    async def _resync(self, http: httpx.AsyncClient) -> None:
        while True:
            response = await http.get(
                f"session/{self.session_id}/history",
                params={"after": self._seq, "limit": 200},
            )
            response.raise_for_status()
            page = response.json()
            for envelope in page["data"]:
                self._publish(envelope)
            if not page["hasMore"]:
                break
        messages: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, str | int] = {"order": "asc", "limit": 200}
            if cursor is not None:
                params["cursor"] = cursor
            response = await http.get(
                f"session/{self.session_id}/message", params=params
            )
            response.raise_for_status()
            page = response.json()
            messages.extend(page["data"])
            cursor = page["cursor"].get("next")
            if not cursor:
                break
        self.events.publish("client.resynced", {"messages": messages, "seq": self._seq})

    def _publish(self, envelope: dict[str, Any]) -> None:
        durable = envelope.get("durable")
        if durable:
            seq = int(durable["seq"])
            if seq <= self._seq:
                return
            self._seq = seq
        event = event_from_sse(envelope)
        self.events.publish(event["type"], event["payload"])
