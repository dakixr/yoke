"""MCP server manager used by yoke tools and CLI commands."""

# ruff: noqa: E501

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from yoke.mcp.client import JSON
from yoke.mcp.client import create_mcp_client
from yoke.mcp.client import McpClient
from yoke.mcp.client import McpClientError
from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import McpConfig
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import load_mcp_config
from yoke.mcp.config import McpSessionPolicy
from yoke.mcp.config import server_supports_tool
from yoke.mcp.inspection import matches_server
from yoke.mcp.inspection import matches_tool
from yoke.mcp.inspection import tool_summary
from yoke.mcp.results import bounded_structured_content
from yoke.mcp.results import mcp_result_text
from yoke.mcp.results import truncate_result_text


class McpManager:
    """Own MCP clients for configured servers."""

    def __init__(
        self,
        config: McpConfig,
        *,
        root: Path,
        config_loader: Callable[[], McpConfig] | None = None,
    ) -> None:
        self.config = config
        self.root = root.resolve()
        self._config_loader = config_loader
        self._config_lock = threading.RLock()
        self._clients: dict[str, McpClient] = {}
        self._clients_lock = threading.Lock()
        self._server_locks: dict[str, threading.Lock] = {}
        self._closed = False

    @classmethod
    def from_paths(
        cls,
        *,
        root: Path,
        home: Path,
        session_policy: McpSessionPolicy | None = None,
    ) -> McpManager:
        """Create a manager from global and workspace config files."""

        def load() -> McpConfig:
            return load_mcp_config(
                root=root,
                home=home,
                session_policy=session_policy,
            )

        return cls(load(), root=root, config_loader=load)

    @property
    def servers(self) -> tuple[McpServerConfig, ...]:
        """Return enabled servers."""
        with self._config_lock:
            return self.config.enabled_servers

    def has_servers(self) -> bool:
        """Return whether any enabled MCP server is configured."""
        return bool(self.servers)

    def close(self) -> None:
        """Close all active clients."""
        first_error: BaseException | None = None
        with self._config_lock:
            with self._clients_lock:
                self._closed = True
                server_names = tuple(self._clients)
            for server_name in server_names:
                with self._server_lock(server_name):
                    client = self._pop_client(server_name)
                    if client is None:
                        continue
                    try:
                        client.close()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
        if first_error is not None:
            raise first_error

    def _refresh_config(self) -> str | None:
        with self._config_lock:
            return self._refresh_config_locked()

    def _refresh_config_locked(self) -> str | None:
        if self._config_loader is None:
            return None
        try:
            refreshed = self._config_loader()
        except Exception as exc:
            return f"MCP config reload failed: {exc}"
        if refreshed == self.config:
            return None

        previous = {server.name: server for server in self.config.servers}
        current = {server.name: server for server in refreshed.servers}
        with self._clients_lock:
            client_names = tuple(self._clients)
        changed = [
            name for name in client_names if previous.get(name) != current.get(name)
        ]
        first_error: Exception | None = None
        for server_name in sorted(changed):
            with self._server_lock(server_name):
                client = self._pop_client(server_name)
                if client is None:
                    continue
                try:
                    client.close()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        self.config = refreshed
        if first_error is not None:
            return (
                f"MCP config reloaded, but closing an old client failed: {first_error}"
            )
        return None

    def status_text(self, server_name: str | None = None) -> str:
        """Return human-readable MCP status."""
        payload = self.inspect(server=server_name)
        if not payload.get("ok"):
            return str(payload.get("error", "MCP error"))
        servers = payload.get("servers")
        if not isinstance(servers, list) or not servers:
            paths = (
                ", ".join(str(path) for path in self.config.paths) or "no config files"
            )
            return f"MCP: no enabled servers ({paths})"
        lines = [f"MCP: {len(servers)} server(s)"]
        for item in servers:
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, object], item)
            status = item.get("status", "unknown")
            name = item.get("name", "unknown")
            tools = item.get("tools", [])
            tool_count = len(tools) if isinstance(tools, list) else 0
            error = item.get("error")
            suffix = f" — {error}" if error else ""
            lines.append(f"  {name}: {status}, {tool_count} tool(s){suffix}")
            if isinstance(tools, list):
                for tool in tools[:25]:
                    if isinstance(tool, dict):
                        description = str(tool.get("description") or "").strip()
                        description = f" — {description}" if description else ""
                        lines.append(f"    - {tool.get('name')}{description}")
                if len(tools) > 25:
                    lines.append(f"    ... {len(tools) - 25} more")
        return "\n".join(lines)

    def inspect(
        self,
        *,
        server: str | None = None,
        query: str | None = None,
        include_schemas: bool = False,
    ) -> dict[str, object]:
        """Inspect configured servers and compact tool metadata."""
        reload_error = self._refresh_config()
        if reload_error is not None:
            return {
                "ok": False,
                "error": reload_error,
                "config_paths": [str(path) for path in self.config.paths],
            }
        selected = self._selected_servers(server)
        if server is not None and not selected:
            return {
                "ok": False,
                "error": f"Unknown or disabled MCP server: {server}",
            }
        needle = query.lower().strip() if query else None
        servers: list[dict[str, object]] = []
        for config in selected:
            server_matches_query = matches_server(config, needle)
            entry: dict[str, object] = {
                "name": config.name,
                "transport": config.transport,
                "enabled": config.enabled,
                "status": "configured",
            }
            if config.transport not in {"stdio", "streamable-http", "http"}:
                entry.update(
                    {
                        "status": "unsupported",
                        "error": f"transport `{config.transport}` is not supported yet",
                        "tools": [],
                    }
                )
                servers.append(entry)
                continue
            try:
                server_lock = self._acquire_configured_server(config)
                if server_lock is None:
                    raise McpClientError(
                        f"MCP server configuration changed during inspection: {config.name}"
                    )
                try:
                    tools = [
                        tool
                        for tool in self._client(config).list_tools()
                        if server_supports_tool(config, tool.name)
                        and (server_matches_query or matches_tool(tool, needle))
                    ]
                finally:
                    server_lock.release()
                entry.update(
                    {
                        "status": "ready",
                        "tools": [
                            tool_summary(tool, include_schema=include_schemas)
                            for tool in tools[:100]
                        ],
                        "truncated": len(tools) > 100,
                    }
                )
            except Exception as exc:
                entry.update({"status": "error", "error": str(exc), "tools": []})
            servers.append(entry)
        return {
            "ok": True,
            "servers": servers,
            "config_paths": [str(path) for path in self.config.paths],
        }

    def call_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: JSON,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Call a configured MCP server tool and compact the result."""
        config, server_lock, reload_error = self._acquire_server(server)
        if reload_error is not None:
            return {"ok": False, "error": reload_error}
        if config is None:
            return {
                "ok": False,
                "error": f"Unknown or disabled MCP server: {server}",
            }
        try:
            if server_lock is None:  # pragma: no cover - config and lock are paired
                raise McpClientError(f"MCP server lock unavailable: {server}")
            if config.transport not in {"stdio", "streamable-http", "http"}:
                return {
                    "ok": False,
                    "error": f"MCP transport `{config.transport}` is not supported yet",
                }
            if not server_supports_tool(config, tool):
                return {
                    "ok": False,
                    "error": f"MCP tool is disabled: {server}/{tool}",
                }
            client = self._client(config)
            known_tools = {item.name for item in client.list_tools()}
            if tool not in known_tools:
                return {
                    "ok": False,
                    "error": f"Unknown MCP tool: {server}/{tool}",
                }
            result = client.call_tool(
                tool,
                arguments,
                cancel_requested=cancel_requested,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if server_lock is not None:
                server_lock.release()
        text = mcp_result_text(result)
        truncated = truncate_result_text(text, server=server, tool=tool)
        structured = bounded_structured_content(
            result.get("structuredContent"),
            full_output_path=cast(str | None, truncated.get("file")),
        )
        return {
            "ok": not bool(result.get("isError")),
            "server": server,
            "tool": tool,
            "content": truncated["text"],
            "isError": bool(result.get("isError")),
            "structuredContent": structured,
            "truncation": truncated["truncation"],
            **(
                {"full_output_path": truncated["file"]} if truncated.get("file") else {}
            ),
        }

    def list_configured_tools(self, server: McpServerConfig) -> tuple[McpToolInfo, ...]:
        """Return all tools advertised by a configured server."""
        config, server_lock, reload_error = self._acquire_server(server.name)
        if reload_error is not None:
            raise McpClientError(reload_error)
        if config is None:
            raise McpClientError(f"Unknown or disabled MCP server: {server.name}")
        if config.transport not in {"stdio", "streamable-http", "http"}:
            if server_lock is not None:
                server_lock.release()
            return ()
        if server_lock is None:  # pragma: no cover - config and lock are paired
            raise McpClientError(f"MCP server lock unavailable: {server.name}")
        try:
            return tuple(self._client(config).list_tools())
        finally:
            server_lock.release()

    def _acquire_server(
        self, name: str
    ) -> tuple[McpServerConfig | None, threading.Lock | None, str | None]:
        with self._config_lock:
            reload_error = self._refresh_config_locked()
            if reload_error is not None:
                return None, None, reload_error
            config = self._server_unlocked(name)
            if config is None:
                return None, None, None
            lock = self._server_lock(name)
            lock.acquire()
            return config, lock, None

    def _acquire_configured_server(
        self, config: McpServerConfig
    ) -> threading.Lock | None:
        with self._config_lock:
            if self._server_unlocked(config.name) != config:
                return None
            lock = self._server_lock(config.name)
            lock.acquire()
            return lock

    def _client(self, server: McpServerConfig) -> McpClient:
        with self._clients_lock:
            if self._closed:
                raise McpClientError("MCP manager is closed")
            client = self._clients.get(server.name)
            if client is None:
                client = create_mcp_client(server, root=self.root)
                self._clients[server.name] = client
            return client

    def _server_lock(self, server_name: str) -> threading.Lock:
        with self._clients_lock:
            lock = self._server_locks.get(server_name)
            if lock is None:
                lock = threading.Lock()
                self._server_locks[server_name] = lock
            return lock

    def _pop_client(self, server_name: str) -> McpClient | None:
        with self._clients_lock:
            return self._clients.pop(server_name, None)

    def _server(self, name: str) -> McpServerConfig | None:
        with self._config_lock:
            return self._server_unlocked(name)

    def _server_unlocked(self, name: str) -> McpServerConfig | None:
        for server in self.config.enabled_servers:
            if server.name == name:
                return server
        return None

    def _selected_servers(self, server: str | None) -> tuple[McpServerConfig, ...]:
        if server is None:
            return self.servers
        found = self._server(server)
        return () if found is None else (found,)
