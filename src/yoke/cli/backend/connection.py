"""HTTP and reconnecting session events for a terminal attached to the daemon."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

import httpx

from yoke.cli.backend.events import EventSubscriptions, event_from_sse
from yoke.cli.backend.connection_state import (
    ConnectionPhase,
    ConnectionSnapshot,
    ConnectionState,
)
from yoke.cli.backend.session_view import SessionViewStore
from yoke.daemon.client import DaemonHandle, connect_or_spawn, unix_client
from yoke.daemon.transport import async_client
from yoke.daemon.uds import ignore_cancelled_socket_wakeups


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
        self.state = ConnectionState(session_id)
        self.owner = f"cli-{uuid4().hex}"
        self.events = EventSubscriptions()
        self.connection_events = EventSubscriptions()
        self.view_store = SessionViewStore(
            session_id, self.owner, input_lifecycle=False
        )
        self._discover = discover
        self._cwd = (cwd or Path.cwd()).resolve()
        self._warning = warning
        self._handle = discover()
        self._warned: set[str] = set()
        self._report_warning()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._worker: Thread | None = None

    def set_cwd(self, directory: Path) -> None:
        """Use the resolved workspace for startup and later reattachments."""
        self._cwd = directory.resolve()

    def select_session(self, session_id: str) -> None:
        """Move this terminal's attachment and stream to another session."""
        if session_id == self.session_id:
            return
        if self._loop is None:
            self.state.switch_session(session_id)
            self.view_store.select_session(session_id)
            return

        async def change() -> None:
            async with async_client(
                self._handle.socket,
                timeout=10,
            ) as http:
                response = await http.delete(
                    f"session/{self.session_id}/client", params={"owner": self.owner}
                )
                response.raise_for_status()
            self.state.switch_session(session_id)
            self.view_store.select_session(session_id)
            if self._stream_task is not None:
                self._stream_task.cancel()

        asyncio.run_coroutine_threadsafe(change(), self._loop).result(timeout=15)
        self.start()

    def client_config(self) -> dict[str, Any]:
        """Take a fresh private environment snapshot, including after restart."""
        return {
            "owner": self.owner,
            "kind": "cli",
            "mode": "full-access",
            "env": dict(os.environ),
            "cwd": str(self._cwd),
        }

    def request(
        self,
        method: str,
        route: str,
        *,
        _identity: ConnectionSnapshot | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Perform one request. In particular, do not replay ambiguous writes."""
        identity = _identity or self.state.snapshot
        handle = self._handle
        try:
            with unix_client(handle.socket, timeout=310) as client:
                response = client.request(method, route, **kwargs)
        except httpx.TransportError:
            if method != "GET" or self._worker is None:
                raise
            self.state.wait_ready(identity.generation, epoch=identity.epoch)
            with unix_client(self._handle.socket, timeout=310) as client:
                response = client.request(method, route, **kwargs)
        if response.is_error:
            raise DaemonRequestError(response)
        return response.json()

    def session_request(
        self, method: str, suffix: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        identity = self.state.snapshot
        return self.request(
            method,
            f"session/{identity.session_id}{suffix}",
            _identity=identity,
            **kwargs,
        )

    @contextmanager
    def startup_lease(self) -> Iterator[None]:
        """Cover configuration discovery before the session stream can open."""
        with unix_client(self._handle.socket, timeout=310) as client:
            with client.stream(
                "GET", "daemon/lease", params={"client": self.owner}
            ) as response:
                response.raise_for_status()
                lines = response.iter_lines()
                next(lines)
                yield

    def start(self, *, timeout: float = 10) -> None:
        """Attach and wait until the stream owns its lease and recovery is done."""
        if self.state.closed:
            raise RuntimeError("Session connection is closed.")
        if self._worker is None:
            self._worker = Thread(target=self._run, daemon=True, name="yoke-cli-events")
            self._worker.start()
        try:
            self.state.wait_ready(timeout=timeout)
        except TimeoutError:
            self.close()
            raise TimeoutError("Could not attach to Yoke daemon events.")

    @property
    def session_id(self) -> str:
        return self.state.snapshot.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.select_session(value)

    @property
    def _closed(self) -> ConnectionState:
        """Read-only compatibility alias; ConnectionState owns closure."""
        return self.state

    @property
    def generation(self) -> int:
        return self.state.snapshot.generation

    def wait_for_recovery(self, after: int, *, timeout: float = 10) -> None:
        """Wait for a complete reattachment, including its authoritative view."""
        self.state.wait_ready(after, timeout=timeout)

    def _enter(self, phase: ConnectionPhase, cause: str) -> None:
        if self.state.closed:
            return
        value = self.state.enter(phase, cause=cause)
        payload: dict[str, object] = {
            "phase": value.phase.value,
            "generation": value.generation,
        }
        self.view_store.apply_event({"type": "client.connection", "payload": payload})
        self.connection_events.publish("client.connection", payload)

    def close(self) -> None:
        """Release only this client's attachment and stream lease."""
        self._enter(ConnectionPhase.CLOSED, "close")
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
        ignore_cancelled_socket_wakeups(self._loop)
        self._task = asyncio.current_task()
        try:
            while not self.state.closed:
                try:
                    self._enter(ConnectionPhase.DISCOVERING, "discover")
                    discovery = asyncio.create_task(asyncio.to_thread(self._discover))
                    try:
                        self._handle = await asyncio.shield(discovery)
                    except asyncio.CancelledError:
                        # A cancelled to_thread keeps running. Join discovery so
                        # close cannot race a late spawn against test cleanup.
                        self._handle = await discovery
                        raise
                    self._report_warning()
                    self._enter(ConnectionPhase.ATTACHING, "discovered")
                    async with async_client(
                        self._handle.socket,
                        timeout=httpx.Timeout(10, read=None),
                    ) as http:
                        self._stream_task = asyncio.create_task(
                            self._attached_stream(http)
                        )
                        try:
                            await self._stream_task
                        except asyncio.CancelledError:
                            if self.state.closed:
                                raise
                        finally:
                            self._stream_task = None
                except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                    if self.state.snapshot.phase == ConnectionPhase.READY:
                        self.events.publish(
                            "client.disconnected", {"message": str(exc)}
                        )
                self._enter(ConnectionPhase.DISCONNECTED, "stream_ended")
                if not self.state.closed:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            # Do not discover or spawn a daemon just to detach on exit.
            try:
                async with async_client(
                    self._handle.socket,
                    timeout=2,
                ) as http:
                    await http.delete(
                        f"session/{self.session_id}/client",
                        params={"owner": self.owner},
                    )
            except (httpx.HTTPError, OSError, ValueError):
                pass
            self.events.close()
            self.connection_events.close()

    async def _attached_stream(self, http: httpx.AsyncClient) -> None:
        response = await http.put(
            f"session/{self.session_id}/client", json=self.client_config()
        )
        response.raise_for_status()
        capabilities = await http.get("capabilities")
        capabilities.raise_for_status()
        self.view_store.input_lifecycle = bool(
            capabilities.json()
            .get("data", capabilities.json())
            .get("features", {})
            .get("inputLifecycle", False)
        )
        self._enter(ConnectionPhase.STREAMING, "attached")
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
                    else:
                        self._publish(envelope)
                        if self.view_store.view.resync_pending:
                            await self._resync(http)

    async def _resync(self, http: httpx.AsyncClient) -> None:
        self._enter(ConnectionPhase.SYNCING, "resync")
        while True:
            response = await http.get(
                f"session/{self.session_id}/history",
                params={"after": self.state.snapshot.seq, "limit": 200},
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
        active_response = await http.get("session/active")
        active_response.raise_for_status()
        active = active_response.json()["data"].get(self.session_id, {"state": "idle"})
        if self.view_store.input_lifecycle:
            inputs = await http.get(
                f"session/{self.session_id}/input", params={"live": "true"}
            )
            inputs.raise_for_status()
            self.view_store.install_snapshot(inputs.json()["data"], active)
        else:
            queue = await http.get(f"session/{self.session_id}/queue")
            queue.raise_for_status()
            self.view_store.install_legacy_snapshot(queue.json()["data"], active)
        self._enter(ConnectionPhase.READY, "resynced")
        self.events.publish(
            "client.resynced", {"messages": messages, "seq": self.state.snapshot.seq}
        )

    def _publish(self, envelope: dict[str, Any]) -> None:
        durable = envelope.get("durable")
        if durable:
            seq = int(durable["seq"])
            if not self.state.advance(self.session_id, seq):
                return
        event = event_from_sse(envelope)
        rendered_locally = self.view_store.owns_input(
            str(event["payload"].get("inputID", ""))
        )
        self.view_store.apply_event(event)
        event["payload"]["renderedLocally"] = rendered_locally
        self.events.publish(event["type"], event["payload"])
