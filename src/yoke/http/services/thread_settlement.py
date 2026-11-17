"""Reconcile T3 settlement through its injected, thread-scoped MCP service.

ACP close has no settlement reason. Keep this observer separate from client
tool configuration so an idle ACP disconnect cannot lose a later settlement.
Credentials remain process-local and are discarded on replacement or revocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx

from yoke.http.errors import ApiError, session_busy, session_not_found
from yoke.mcp.client import McpClientError, StreamableHttpClient
from yoke.mcp.config import McpServerConfig
from yoke.session.workspace import workspace_lease

if TYPE_CHECKING:
    from yoke.http.models.client_session import ClientSessionRequest
    from yoke.http.runtime.registry import SessionRuntimeRegistry
    from yoke.http.services.session_service import SessionService

LOGGER = logging.getLogger(__name__)


@dataclass
class ThreadBinding:
    owner: str
    server: McpServerConfig = field(repr=False)
    thread_id: str | None = None
    archived_by_sync: bool = False
    failures: int = 0
    next_read_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        raise McpClientError("T3 thread read failed")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if item.get("type") == "text":
            data = json.loads(item["text"])
            if isinstance(data, dict):
                return data
    raise McpClientError("T3 thread read returned no structured state")


class SettlementReadError(McpClientError):
    """A safe capability label and retry policy, never remote response text."""

    def __init__(
        self, capability: str, *, transient: bool = False, reason: str = "read failed"
    ) -> None:
        super().__init__(reason)
        self.capability = capability
        self.transient = transient


def _transient(exc: Exception) -> bool:
    if isinstance(exc, SettlementReadError):
        return exc.transient
    if isinstance(exc, McpClientError):
        # The HTTP client supplies status codes but discards JSON-RPC error codes.
        # All other MCP errors, including unknown tools, are permanent.
        return str(exc).startswith(
            tuple(
                f"MCP HTTP {kind} failed: {status} "
                for kind in ("request", "notification")
                for status in (408, 425, 429, *range(500, 600))
            )
        )
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


def read_thread(binding: ThreadBinding) -> dict[str, Any]:
    """Discover scoped identity from the catalogue, outside the model's tools."""
    client = StreamableHttpClient(binding.server, root=binding.server.cwd or Path.cwd())
    capability = "tools/list"
    try:
        tools = {tool.name: tool for tool in client.list_tools()}
        capability = "t3_thread_read"
        if capability not in tools:
            raise SettlementReadError(capability)
        thread_id = binding.thread_id
        if thread_id is None:
            capability = "t3_thread_configure or t3_thread_configuration"
            identity_tool = next(
                (
                    name
                    for name in ("t3_thread_configure", "t3_thread_configuration")
                    if name in tools and not tools[name].input_schema.get("required")
                ),
                None,
            )
            if identity_tool is None:
                raise SettlementReadError(capability)
            capability = identity_tool
            thread_id = _result(client.call_tool(identity_tool, {})).get("threadId")
            if not isinstance(thread_id, str) or not thread_id:
                raise SettlementReadError(capability)
        capability = "t3_thread_read"
        data = _result(
            client.call_tool(
                capability,
                {"threadId": thread_id, "limit": 1, "runLimit": 1},
            )
        ).get("thread")
        if not isinstance(data, dict) or data.get("threadId") != thread_id:
            raise SettlementReadError(capability, reason="T3 thread identity mismatch")
        return data
    except SettlementReadError:
        raise
    except Exception as exc:
        raise SettlementReadError(capability, transient=_transient(exc)) from None
    finally:
        client.close()


class ThreadSettlementService:
    def __init__(
        self, sessions: SessionService, runtimes: SessionRuntimeRegistry
    ) -> None:
        self.sessions = sessions
        self.runtimes = runtimes
        self.bindings: dict[str, ThreadBinding] = {}
        self.interval = 15.0
        self._slots = asyncio.Semaphore(4)

    def attach(self, session_id: str, config: ClientSessionRequest, root: Path) -> None:
        for server in config.mcp_servers:
            if server.name != "t3-code":
                continue
            endpoint = server.env.get("T3_ACP_MCP_ENDPOINT")
            authorization = server.env.get("T3_ACP_MCP_AUTHORIZATION")
            if server.transport == "streamable-http":
                endpoint = server.url
                authorization = next(
                    (
                        v
                        for k, v in server.headers.items()
                        if k.lower() == "authorization"
                    ),
                    None,
                )
            if not endpoint or not authorization:
                continue
            if not endpoint.startswith(("http://", "https://")):
                continue
            previous = self.bindings.get(session_id)
            if previous is not None and (
                previous.owner != config.owner
                or previous.server.url != endpoint
                or previous.server.headers != {"Authorization": authorization}
            ):
                previous = None
            if previous is not None:
                # Reattaching the same credential must not reset backoff or its lock.
                return
            self.bindings[session_id] = ThreadBinding(
                owner=config.owner,
                server=McpServerConfig(
                    name="t3-code",
                    transport="streamable-http",
                    url=endpoint,
                    headers={"Authorization": authorization},
                    cwd=root,
                    startup_timeout_sec=3,
                    tool_timeout_sec=3,
                ),
            )
            return
        previous = self.bindings.get(session_id)
        if previous is not None and previous.owner == config.owner:
            self.bindings.pop(session_id, None)

    async def reconcile(self, session_id: str, owner: str | None = None) -> None:
        binding = self.bindings.get(session_id)
        if binding is None or (owner is not None and binding.owner != owner):
            return
        async with binding.lock, self._slots:
            if self.bindings.get(session_id) is not binding:
                return
            if monotonic() < binding.next_read_at:
                return
            try:
                if self.sessions.store.summary_record(session_id) is None:
                    self.bindings.pop(session_id, None)
                    return
                data = await asyncio.to_thread(read_thread, binding)
                if self.bindings.get(session_id) is not binding:
                    return
                binding.failures = 0
                binding.next_read_at = 0.0
                binding.thread_id = data["threadId"]
                settled = data.get("settled") is True or data.get("archived") is True
                active = data.get("settled") is False and data.get("archived") is False
                # Only undo archives this observer applied. Missing state is unknown.
                if not settled and (not active or not binding.archived_by_sync):
                    return

                def synchronize() -> None:
                    if self.bindings.get(session_id) is not binding:
                        return
                    record = self.sessions.store.summary_record(session_id)
                    if record is None:
                        raise session_not_found()
                    if (record.archived_at is not None) == settled:
                        if not settled:
                            binding.archived_by_sync = False
                        return
                    runtime = self.runtimes.get_if_loaded(session_id)
                    if (
                        settled
                        and runtime is not None
                        and set(runtime.resources.configuration.clients.owners())
                        - {binding.owner}
                    ):
                        return
                    if settled and runtime is not None and runtime.workers.active_count:
                        raise session_busy(
                            "Native workers must drain before settlement."
                        )
                    with workspace_lease(self.sessions.store, session_id):
                        self.sessions.patch_session(
                            session_id,
                            title_set=False,
                            title=None,
                            pinned_set=False,
                            pinned=None,
                            archived_set=True,
                            archived=settled,
                        )
                        binding.archived_by_sync = settled

                await self.runtimes.get_or_start(session_id).idle_mutation(synchronize)
            except ApiError as exc:
                if exc.status_code == 404 and self.bindings.get(session_id) is binding:
                    self.bindings.pop(session_id, None)
                # Busy/queued sessions are retried after their native work drains.
            except Exception as exc:
                if self.bindings.get(session_id) is not binding:
                    return
                # Never log credentials, tool output, endpoint URLs or exception text.
                capability = (
                    exc.capability if isinstance(exc, SettlementReadError) else "read"
                )
                permanent = isinstance(exc, McpClientError) and not _transient(exc)
                if permanent:
                    self.bindings.pop(session_id, None)
                    LOGGER.warning(
                        "T3 settlement sync disabled for %s provider %s: "
                        "capability %s unavailable or invalid (%s)",
                        session_id,
                        binding.server.name,
                        capability,
                        type(exc).__name__,
                    )
                    return
                binding.failures = min(binding.failures + 1, 5)
                delay = min(300, 30 * 2 ** (binding.failures - 1))
                binding.next_read_at = monotonic() + delay
                if binding.failures == 1:
                    LOGGER.warning(
                        "T3 settlement read failed for %s provider %s: "
                        "capability %s; retrying with backoff (%s)",
                        session_id,
                        binding.server.name,
                        capability,
                        type(exc).__name__,
                    )

    async def watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                await asyncio.gather(
                    *(self.reconcile(key) for key in list(self.bindings))
                )
        finally:
            self.bindings.clear()
