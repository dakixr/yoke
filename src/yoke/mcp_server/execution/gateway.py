"""Complete discovery and raw gateway results for the remote MCP profile only."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from yoke.mcp.config import server_supports_tool
from yoke.mcp.manager import McpManager
from yoke.mcp_server.execution.models import Inspect


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def inspect(manager: McpManager, request: Inspect) -> dict[str, Any]:
    error = manager._refresh_config()
    if error:
        raise ValueError(error)
    configs = [
        s for s in manager.servers if request.server is None or s.name == request.server
    ]
    if request.server and not configs:
        raise ValueError("Unknown or disabled MCP server")
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    needles = [q.lower() for q in [request.query, *request.queries] if q]
    for config in configs:
        try:
            for tool in manager.list_configured_tools(config, force=request.refresh):
                if not server_supports_tool(config, tool.name):
                    continue
                if request.tools and tool.name not in request.tools:
                    continue
                if needles and not any(
                    q in f"{config.name} {tool.name} {tool.description}".lower()
                    for q in needles
                ):
                    continue
                entries.append(
                    {
                        "server": config.name,
                        "name": tool.name,
                        "description": tool.description,
                        "schema_hash": schema_hash(tool.input_schema),
                        "annotations": tool.annotations,
                        "effects": "unknown"
                        if tool.annotations is None
                        else "advertised",
                        **(
                            {
                                "input_schema": tool.input_schema,
                                "output_schema": tool.output_schema,
                            }
                            if request.include_schemas
                            else {}
                        ),
                    }
                )
        except Exception as exc:
            errors.append({"server": config.name, "error": str(exc)})
    entries.sort(key=lambda item: (item["server"], item["name"]))
    catalog_hash = schema_hash({"entries": entries, "errors": errors})
    start = 0
    if request.cursor:
        digest, offset = request.cursor.split(":", 1)
        if digest != catalog_hash:
            raise ValueError("Catalog changed; restart discovery without a cursor")
        start = int(offset)
        if start < 0 or start > len(entries):
            raise ValueError("Invalid discovery cursor")
    page = entries[start : start + request.limit]
    end = start + len(page)
    servers = [
        {
            "name": config.name,
            "status": "error"
            if any(e["server"] == config.name for e in errors)
            else "ready",
            "tools": [item for item in page if item["server"] == config.name],
        }
        for config in configs
    ]
    return {
        "ok": not errors,
        "servers": servers,
        "config_paths": [str(p) for p in manager.config.paths],
        "errors": errors,
        "total": len(entries),
        "catalog_hash": catalog_hash,
        "next_cursor": f"{catalog_hash}:{end}" if end < len(entries) else None,
    }


def call(
    manager: McpManager,
    *,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    expected_hash: str | None = None,
    cancel_requested: Any = None,
) -> dict[str, Any]:
    # Validate the selected schema under the same server lease as dispatch.
    config, lock, error = manager._acquire_server(server)
    if error or config is None or lock is None:
        raise ValueError(error or "Unknown or disabled MCP server")
    try:
        if not server_supports_tool(config, tool):
            raise ValueError(f"MCP tool is disabled: {server}/{tool}")
        client = manager._client(config)
        selected = next(
            (t for t in client.list_tools(force=bool(expected_hash)) if t.name == tool),
            None,
        )
        if selected is None:
            raise ValueError("Unknown downstream tool")
        if expected_hash and expected_hash != schema_hash(selected.input_schema):
            raise ValueError("Tool schema changed; inspect again")
        from jsonschema import Draft202012Validator

        Draft202012Validator(selected.input_schema).validate(arguments)
        try:
            result = client.call_tool(
                tool, arguments, cancel_requested=cancel_requested
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "unknown",
                "server": server,
                "tool": tool,
                "error": str(exc),
                "retry": "inspect_outcome_before_retry",
            }
        return {
            "ok": not result.get("isError", False),
            "server": server,
            "tool": tool,
            **result,
        }
    finally:
        lock.release()
