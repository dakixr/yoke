"""Translate between MCP requests and bound Yoke tools."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from pathlib import Path

from mcp.types import CallToolResult
from mcp.types import ImageContent
from mcp.types import TextContent
from mcp.types import Tool
from pydantic import ValidationError

from yoke.mcp_server.execution.service import ExecutionService
from yoke.mcp_server.results.encoding import encode
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.process_runtime import ProcessRuntime
from yoke.mcp.manager import McpManager
from yoke.mcp_server.registry import effective_tool_registry
from yoke.mcp_server.registry import ExposedTool

logger = logging.getLogger(__name__)


class ToolAdapter:
    """Advertise and execute the fixed Yoke MCP tool surface."""

    def __init__(
        self,
        config: MCPServerConfig,
        runtime: ProcessRuntime,
        downstream_manager: McpManager,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.downstream_manager = downstream_manager
        self._registry = effective_tool_registry()
        self.execution = ExecutionService(config, runtime, downstream_manager)

    def list_tools(self) -> list[Tool]:
        """Return exact Pydantic schemas with stable external tool names."""
        tools = {spec.name: self._mcp_tool(spec) for spec in self._registry.values()}
        tools.update(self.execution.tools())
        return list(tools.values())

    async def call_tool(
        self, name: str, arguments: dict[str, object] | None
    ) -> CallToolResult:
        """Validate, bind, execute, and encode one tool call."""
        started = time.monotonic()
        if self.execution.accepts(name):
            try:
                result = await self.execution.dispatch(name, arguments or {})
                raw_budget = (arguments or {}).get("max_output_tokens")
                budget = (
                    min(64000, raw_budget * 4) if isinstance(raw_budget, int) else 32000
                )
                if name == "result_read":
                    budget = 150000
                elif name == "process_read":
                    budget = 400000
                elif name == "export_file":
                    budget = 3 * 1024 * 1024
                encoded = encode(
                    result,
                    self.execution.store,
                    budget=budget,
                    legacy_text=self.config.legacy_result_text,
                )
            except ValidationError as exc:
                return self._error(_validation_message(exc))
            except Exception as exc:
                return self._error(str(exc))
            self._log_call(
                name,
                round((time.monotonic() - started) * 1000),
                bool(result.get("ok", True)),
            )
            return encoded
        spec = self._registry.get(name)
        if spec is None:
            return self._error(f"Unknown tool: {name}")
        parsed_arguments = self._with_runtime_defaults(name, arguments or {})
        binding: dict[str, object] = {
            "root": self.config.root,
            "command_process_manager": self.runtime.manager,
            "skill_dirs": self.config.skill_dirs,
        }
        binding["mcp_manager"] = self.downstream_manager
        prototype = spec.tool_class.bind(
            **binding,
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
        try:
            encoded = _encode_tool_result(spec, result)
        except (TypeError, ValueError) as exc:
            logger.exception("MCP tool result encoding crashed", extra={"tool": name})
            result = {"ok": False, "error": f"Invalid internal tool result: {exc}"}
            encoded = _encode_json_result(result)
        duration_ms = round((time.monotonic() - started) * 1000)
        self._log_call(name, duration_ms, bool(result.get("ok", False)))
        return encoded

    def _mcp_tool(self, spec: ExposedTool) -> Tool:
        schema = spec.tool_class.model_json_schema(by_alias=True)
        if spec.name == "exec_command":
            schema["properties"]["login"]["default"] = False
            schema["properties"]["yield_time_ms"]["default"] = (
                self.config.default_yield_ms
            )
            schema["properties"]["max_output_tokens"]["default"] = (
                self.config.max_output_tokens
            )
        if spec.name == "process_io":
            schema["properties"]["max_output_tokens"]["default"] = (
                self.config.max_output_tokens
            )
        return Tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            input_schema=schema,
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
        return _encode_json_result({"ok": False, "error": message})


def _encode_tool_result(spec: ExposedTool, result: dict[str, object]) -> CallToolResult:
    if not bool(result.get("ok", True)) or spec.result_kind == "json":
        return _encode_json_result(result)
    if spec.result_kind != "image":
        raise ValueError(f"Unsupported result kind: {spec.result_kind}")
    data = result.get("data_base64")
    mime_type = result.get("mime_type")
    byte_count = result.get("bytes")
    if not isinstance(data, str) or not data:
        raise TypeError("image data_base64 must be a non-empty string")
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        raise TypeError("image mime_type must be an image MIME type")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool):
        raise TypeError("image bytes must be an integer")
    try:
        decoded_size = len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image data_base64 is invalid") from exc
    if decoded_size != byte_count:
        raise ValueError("image byte count does not match data_base64")
    return CallToolResult(
        content=[ImageContent(type="image", data=data, mime_type=mime_type)],
        is_error=False,
    )


def _encode_json_result(result: dict[str, object]) -> CallToolResult:
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
