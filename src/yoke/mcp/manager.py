"""MCP server manager used by yoke tools and CLI commands."""

from __future__ import annotations

import atexit
import functools
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.agent.truncate import DEFAULT_MAX_LINES
from yoke.agent.truncate import format_size
from yoke.agent.truncate import truncate_head
from yoke.mcp.client import create_mcp_client
from yoke.mcp.client import McpClient
from yoke.mcp.types import JSON
from yoke.mcp.types import McpToolInfo
from yoke.mcp.config import McpConfig
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import load_mcp_config
from yoke.mcp.config import McpSessionPolicy
from yoke.mcp.config import server_supports_tool
from yoke.mcp.config import tool_schema_for_inspection


class McpManager:
    """Own MCP clients for configured servers."""

    def __init__(self, config: McpConfig, *, root: Path) -> None:
        self.config = config
        self.root = root.resolve()
        self._clients: dict[str, McpClient] = {}

    @classmethod
    def from_paths(
        cls,
        *,
        root: Path,
        home: Path,
        session_policy: McpSessionPolicy | None = None,
    ) -> McpManager:
        """Create a manager from global and workspace config files."""
        return cls(
            load_mcp_config(
                root=root,
                home=home,
                session_policy=session_policy,
            ),
            root=root,
        )

    @property
    def servers(self) -> tuple[McpServerConfig, ...]:
        """Return enabled servers."""
        return self.config.enabled_servers

    def has_servers(self) -> bool:
        """Return whether any enabled MCP server is configured."""
        return bool(self.servers)

    def close(self) -> None:
        """Close all active clients."""
        clients = list(self._clients.values())
        self._clients.clear()
        errors: list[Exception] = []
        for client in clients:
            try:
                client.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close MCP clients", errors)

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
        selected = self._selected_servers(server)
        if server is not None and not selected:
            return {"ok": False, "error": f"Unknown or disabled MCP server: {server}"}
        needle = query.lower().strip() if query else None
        servers: list[dict[str, object]] = []
        for config in selected:
            server_matches_query = _matches_server(config, needle)
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
                listed_tools = (
                    self._client(config).list_tools()
                    if include_schemas
                    else self._client(config).list_tool_summaries()
                )
                tools = [
                    tool
                    for tool in listed_tools
                    if server_supports_tool(config, tool.name)
                    and (server_matches_query or _matches_tool(tool, needle))
                ]
                entry.update(
                    {
                        "status": "ready",
                        "tools": [
                            _tool_summary(tool, include_schema=include_schemas)
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
        config = self._server(server)
        if config is None:
            return {"ok": False, "error": f"Unknown or disabled MCP server: {server}"}
        if config.transport not in {"stdio", "streamable-http", "http"}:
            return {
                "ok": False,
                "error": f"MCP transport `{config.transport}` is not supported yet",
            }
        if not server_supports_tool(config, tool):
            return {"ok": False, "error": f"MCP tool is disabled: {server}/{tool}"}
        try:
            known_tools = {
                item.name for item in self._client(config).list_tool_summaries()
            }
            if tool not in known_tools:
                return {"ok": False, "error": f"Unknown MCP tool: {server}/{tool}"}
            result = self._client(config).call_tool(
                tool,
                arguments,
                cancel_requested=cancel_requested,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        text = _mcp_result_text(result)
        truncated = _truncate_result_text(text, server=server, tool=tool)
        payload: dict[str, object] = {
            "ok": not bool(result.get("isError")),
            "server": server,
            "tool": tool,
            "content": truncated["text"],
            "isError": bool(result.get("isError")),
            "truncation": truncated["truncation"],
            **(
                {"full_output_path": truncated["file"]} if truncated.get("file") else {}
            ),
        }
        structured_content, structured_truncated = _bounded_structured_content(
            result.get("structuredContent")
        )
        if structured_content is not None:
            payload["structuredContent"] = structured_content
        if structured_truncated:
            payload["structuredContentTruncated"] = True
        return payload

    def list_configured_tools(self, server: McpServerConfig) -> tuple[McpToolInfo, ...]:
        """Return tool names/descriptions advertised by a configured server."""
        if server.transport not in {"stdio", "streamable-http", "http"}:
            return ()
        return tuple(self._client(server).list_tool_summaries())

    def _client(self, server: McpServerConfig) -> McpClient:
        client = self._clients.get(server.name)
        if client is None:
            client = create_mcp_client(server, root=self.root)
            self._clients[server.name] = client
        return client

    def _server(self, name: str) -> McpServerConfig | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None

    def _selected_servers(self, server: str | None) -> tuple[McpServerConfig, ...]:
        if server is None:
            return self.servers
        found = self._server(server)
        return () if found is None else (found,)


def _matches_server(server: McpServerConfig, needle: str | None) -> bool:
    if needle is None:
        return True
    return needle in server.name.lower()


def _matches_tool(tool: McpToolInfo, needle: str | None) -> bool:
    if needle is None:
        return True
    return needle in tool.name.lower() or needle in tool.description.lower()


def _tool_summary(tool: McpToolInfo, *, include_schema: bool) -> dict[str, object]:
    description = " ".join(tool.description.split())
    if len(description) > 240:
        description = description[:239].rstrip() + "…"
    summary: dict[str, object] = {"name": tool.name, "description": description}
    schema = tool_schema_for_inspection(
        tool.input_schema,
        include_schema=include_schema,
    )
    if schema is not None:
        summary["input_schema"] = schema
    return summary


def _mcp_result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            parts.append(_content_part_text(item))
    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(
            "Structured content:\n"
            + json.dumps(structured, indent=2, ensure_ascii=False)
        )
    if not parts:
        return json.dumps(result, indent=2, ensure_ascii=False)
    return "\n\n".join(part for part in parts if part)


def _content_part_text(item: object) -> str:
    if not isinstance(item, dict):
        return str(item)
    item_type = item.get("type")
    if item_type == "text":
        text = item.get("text")
        return text if isinstance(text, str) else ""
    if item_type == "image":
        return f"[Image result: {item.get('mimeType', 'unknown')}]"
    if item_type == "audio":
        return f"[Audio result: {item.get('mimeType', 'unknown')}]"
    if item_type == "resource":
        resource = item.get("resource")
        if isinstance(resource, dict):
            uri = resource.get("uri", "unknown")
            text = resource.get("text")
            if isinstance(text, str):
                return f"[Resource: {uri}]\n{text}"
            return f"[Resource: {uri}]"
    return json.dumps(item, ensure_ascii=False)


def _truncate_result_text(text: str, *, server: str, tool: str) -> dict[str, object]:
    truncation = truncate_head(
        text,
        max_lines=DEFAULT_MAX_LINES,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    file_path: str | None = None
    content = truncation.content
    if truncation.truncated:
        safe = _safe_output_prefix(server=server, tool=tool)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=safe,
            suffix=".txt",
            dir=_private_output_dir(),
            delete=False,
        ) as handle:
            handle.write(text)
            file_path = handle.name
        content = (
            content
            + "\n\n"
            + "[MCP output truncated: "
            + f"{truncation.output_lines} of {truncation.total_lines} lines, "
            + f"{format_size(truncation.output_bytes)} of {format_size(truncation.total_bytes)}. "
            + f"Full output saved to: {file_path}]"
        )
    return {
        "text": content,
        "file": file_path,
        "truncation": truncation.to_dict(),
    }


def _bounded_structured_content(value: object) -> tuple[object | None, bool]:
    if value is None:
        return None, False
    try:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None, True
    if len(encoded) > DEFAULT_MAX_BYTES:
        return None, True
    return value, False


def _safe_output_prefix(*, server: str, tool: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{server}-{tool}")
    prefix = prefix.strip("._-")[:80] or "result"
    return f"{prefix}-"


@functools.lru_cache(maxsize=1)
def _private_output_dir() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="yoke-mcp-"))
    directory.chmod(0o700)
    return directory


def _cleanup_private_output_dir() -> None:
    if _private_output_dir.cache_info().currsize:
        shutil.rmtree(_private_output_dir(), ignore_errors=True)


atexit.register(_cleanup_private_output_dir)
