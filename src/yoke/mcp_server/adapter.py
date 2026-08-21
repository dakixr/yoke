"""Translate between MCP requests and bound Yoke tools."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from mcp.types import CallToolResult
from mcp.types import TextContent
from mcp.types import Tool
from pydantic import ValidationError

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.process_runtime import ProcessRuntime
from yoke.mcp_server.registry import TOOL_REGISTRY
from yoke.mcp_server.registry import ExposedTool
from yoke.mcp_server.skills import load_mcp_skill_registry

logger = logging.getLogger(__name__)


class ToolAdapter:
    """Advertise and execute the fixed Yoke MCP tool surface."""

    def __init__(self, config: MCPServerConfig, runtime: ProcessRuntime) -> None:
        self.config = config
        self.runtime = runtime
        self.skill_registry = load_mcp_skill_registry(config.skill_dirs)

    def list_tools(self) -> list[Tool]:
        """Return exact Pydantic schemas with stable external tool names."""
        return [self._mcp_tool(spec) for spec in TOOL_REGISTRY.values()]

    async def call_tool(
        self, name: str, arguments: dict[str, object] | None
    ) -> CallToolResult:
        """Validate, bind, execute, and encode one tool call."""
        started = time.monotonic()
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            return self._error(f"Unknown tool: {name}")
        parsed_arguments = self._with_runtime_defaults(name, arguments or {})
        prototype = spec.tool_class.bind(
            root=self.config.root,
            command_process_manager=self.runtime.manager,
            skill_registry=self.skill_registry,
        )
        try:
            tool = prototype.parse_arguments(parsed_arguments)
        except ValidationError as exc:
            return self._error(_validation_message(exc))
        result: dict[str, object]
        try:
            result = await self.runtime.execute(name, tool)
        except Exception as exc:  # pragma: no cover - final adapter boundary
            logger.exception("MCP tool execution crashed", extra={"tool": name})
            result = {"ok": False, "error": str(exc)}
        duration_ms = round((time.monotonic() - started) * 1000)
        self._log_call(name, duration_ms, bool(result.get("ok", False)))
        return _encode_result(result)

    def _mcp_tool(self, spec: ExposedTool) -> Tool:
        return Tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            input_schema=spec.tool_class.model_json_schema(by_alias=True),
            annotations=spec.annotations,
        )

    def _with_runtime_defaults(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        values = dict(arguments)
        if name in {"exec_command", "exec_python"}:
            values.setdefault("yield_time_ms", self.config.default_yield_ms)
            values.setdefault("max_output_tokens", self.config.max_output_tokens)
        if name == "exec_command":
            values.setdefault("login", False)
        if name == "exec_python":
            values.setdefault("timeout", self.config.python_timeout)
        if name == "process_io":
            values.setdefault("max_output_tokens", self.config.max_output_tokens)
        return values

    def _log_call(self, name: str, duration_ms: int, ok: bool) -> None:
        logger.info(
            "MCP tool call",
            extra={"tool": name, "duration_ms": duration_ms, "ok": ok},
        )

    @staticmethod
    def _error(message: str) -> CallToolResult:
        return _encode_result({"ok": False, "error": message})


def _encode_result(result: dict[str, object]) -> CallToolResult:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=result,
        is_error=not bool(result.get("ok", True)),
    )


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "Invalid tool arguments: " + "; ".join(parts)


def result_target_path(arguments: dict[str, object]) -> Path | None:
    """Return a path-like audit target without reading or logging file contents."""
    value = arguments.get("path") or arguments.get("workdir")
    return Path(value) if isinstance(value, str) else None
