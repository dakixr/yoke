"""Reconcile T3 settlement through its injected, thread-scoped MCP service.

ACP close has no settlement reason. Keep this observer separate from client
tool configuration so an idle ACP disconnect cannot lose a later settlement.
Credentials remain process-local and are discarded on settlement or revocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yoke.http.errors import ApiError
from yoke.mcp.config import McpServerConfig
from yoke.mcp.client import McpClientError, StreamableHttpClient
from yoke.session.workspace import workspace_lease

if TYPE_CHECKING:
    from yoke.http.models.client_session import ClientSessionRequest
    from yoke.http.services.runtime_registry import SessionRuntimeRegistry
    from yoke.http.services.session_service import SessionService

LOGGER = logging.getLogger(__name__)


@dataclass
class ThreadBinding:
    owner: str
    server: McpServerConfig = field(repr=False)
    thread_id: str | None = None
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


def read_thread(binding: ThreadBinding) -> dict[str, Any]:
    """Read authoritative state without involving the model or its tool cache."""
    client = StreamableHttpClient(binding.server, root=binding.server.cwd or Path.cwd())
    try:
        thread_id = binding.thread_id
        if thread_id is None:
            thread_id = _result(client.call_tool("t3_thread_configuration", {})).get(
                "threadId"
            )
            if not isinstance(thread_id, str) or not thread_id:
                raise McpClientError("T3 did not identify the scoped thread")
        data = _result(
            client.call_tool(
                "t3_thread_read",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "runLimit": 1,
                },
            )
        ).get("thread")
        if not isinstance(data, dict) or data.get("threadId") != thread_id:
            raise McpClientError("T3 thread identity mismatch")
        return data
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
        # A new client without T3 credentials supersedes any old binding.
        self.bindings.pop(session_id, None)

    async def reconcile(self, session_id: str, owner: str | None = None) -> None:
        binding = self.bindings.get(session_id)
        if binding is None or (owner is not None and binding.owner != owner):
            return
        async with binding.lock, self._slots:
            if self.bindings.get(session_id) is not binding:
                return
            try:
                if self.sessions.store.summary_record(session_id) is None:
                    self.bindings.pop(session_id, None)
                    return
                data = await asyncio.to_thread(read_thread, binding)
                if self.bindings.get(session_id) is not binding:
                    return
                binding.thread_id = data["threadId"]
                # Unknown/missing state, failures and ordinary closes never settle.
                if data.get("settled") is not True and data.get("archived") is not True:
                    return

                def archive() -> None:
                    if self.bindings.get(session_id) is not binding:
                        return
                    runtime = self.runtimes.get_if_loaded(session_id)
                    if runtime is not None and runtime.workers.active_count:
                        raise ApiError(
                            409,
                            "session_busy",
                            "Native workers must drain before settlement.",
                        )
                    with workspace_lease(self.sessions.store, session_id):
                        self.sessions.patch_session(
                            session_id,
                            title_set=False,
                            title=None,
                            pinned_set=False,
                            pinned=None,
                            archived_set=True,
                            archived=True,
                        )

                await self.runtimes.idle_mutation(session_id, archive)
                if self.bindings.get(session_id) is binding:
                    self.bindings.pop(session_id, None)
            except ApiError as exc:
                if exc.status_code == 404 and self.bindings.get(session_id) is binding:
                    self.bindings.pop(session_id, None)
                # Busy/queued sessions are retried after their native work drains.
            except Exception as exc:
                # Never log credentials, tool output, or endpoint URLs.
                if isinstance(exc, McpClientError) and any(
                    f"{status} " in str(exc) for status in (401, 403, 404)
                ):
                    if self.bindings.get(session_id) is binding:
                        self.bindings.pop(session_id, None)
                LOGGER.debug(
                    "T3 settlement read failed for %s (%s)",
                    session_id,
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
