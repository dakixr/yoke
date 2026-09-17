"""Translate between MCP requests and bound Yoke tools."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import threading

from mcp.types import CallToolResult
from mcp.types import ImageContent
from mcp.types import TextContent
from mcp.types import Tool
from pydantic import ValidationError

from yoke._version import __version__
from yoke.mcp_server.diagnostics import CallDiagnostics
from yoke.mcp_server.execution import policy
from yoke.mcp_server.execution.service import ExecutionService
from yoke.mcp_server.results.contracts import OUTPUTS
from yoke.mcp_server.results.encoding import encode
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.process_runtime import ProcessRuntime
from yoke.mcp.manager import McpManager
from yoke.mcp_server.registry import effective_tool_registry
from yoke.mcp_server.registry import ExposedTool


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
        """Return client-compatible schemas with stable external tool names."""
        tools = {spec.name: self._mcp_tool(spec) for spec in self._registry.values()}
        tools.update(self.execution.tools())
        return list(tools.values())

    async def call_tool(
        self, name: str, arguments: dict[str, object] | None
    ) -> CallToolResult:
        """Validate, bind, execute, and encode one tool call."""
        known = name in self._registry or self.execution.accepts(name)
        call = CallDiagnostics(name if known else "<unknown>", arguments or {})
        try:
            response = await self._call_tool(name, arguments or {}, call)
        except ValidationError as exc:
            if call.stage == "input_validation":
                response = _encode_json_result(call.invalid_arguments(exc))
            else:
                call.exception_type = type(exc).__name__
                response = _encode_json_result(
                    call.error(
                        "TOOL_EXECUTION_ERROR",
                        "Tool raised a validation error after dispatch.",
                    )
                )
        except Exception as exc:
            call.exception_type = type(exc).__name__
            code = "TOOL_EXECUTION_ERROR"
            message = str(exc)
            if call.stage == "result_encoding":
                code = "INVALID_TOOL_RESULT"
                message = "Invalid internal tool result: " + message
            elif isinstance(exc, PermissionError):
                code = "OS_PERMISSION_DENIED"
            response = _encode_json_result(call.error(code, message))
        except BaseException:
            call.ok = False
            call.outcome = "interrupted"
            raise
        finally:
            call.finish()
        response.meta = {
            **(response.meta or {}),
            "yoke/request_id": call.request_id,
            "yoke/version": __version__,
        }
        return response

    async def _call_tool(
        self, name: str, arguments: dict[str, object], call: CallDiagnostics
    ) -> CallToolResult:
        if self.execution.accepts(name):
            self.execution.validate_arguments(name, arguments)
            call.begin_execution()
            result = await self.execution.dispatch(name, arguments)
            call.observe(result)
            raw_budget = arguments.get("max_output_tokens")
            if name == "python_exec" and raw_budget is None:
                raw_budget = self.config.max_output_tokens
            budget = (
                min(64000, raw_budget * 4) if isinstance(raw_budget, int) else 32000
            )
            if name == "result_read":
                budget = 150000
            elif name == "process_read":
                budget = 400000
            elif name == "export_file":
                budget = 3 * 1024 * 1024
            call.stage = "result_encoding"
            encoded = encode(
                result,
                self.execution.store,
                budget=budget,
                legacy_text=self.config.legacy_result_text,
                batch=name == "batch_read" and bool(result.get("ok")),
                process=name in {"python_exec", "process_read"},
                process_recipe=name == "check_patch",
            )
            call.stage = "complete" if call.ok else "execution"
            return encoded
        spec = self._registry.get(name)
        if spec is None:
            call.stage = "tool_lookup"
            return _encode_json_result(
                call.error("UNKNOWN_TOOL", f"Unknown tool: {name}")
            )
        parsed_arguments = self._with_runtime_defaults(name, arguments)
        cancelled = threading.Event()
        binding: dict[str, object] = {
            "root": self.config.root,
            "command_process_manager": self.runtime.manager,
            "skill_dirs": self.config.skill_dirs,
            "cancel_requested": cancelled.is_set,
            "default_exec_wait_ms": min(
                self.config.default_yield_ms, self.config.max_remote_wait_ms
            ),
        }
        binding["mcp_manager"] = self.downstream_manager
        prototype = spec.tool_class.bind(
            **binding,
        )
        tool = prototype.parse_arguments(parsed_arguments)
        call.begin_execution()
        try:
            result = await self.runtime.execute(name, tool, cancel=cancelled)
        except BaseException:
            cancelled.set()
            raise
        call.observe(result)
        call.stage = "result_encoding"
        raw_budget = parsed_arguments.get("max_output_tokens")
        encoded = (
            encode(
                result,
                self.execution.store,
                budget=min(64000, raw_budget * 4)
                if isinstance(raw_budget, int)
                else 32000,
                legacy_text=self.config.legacy_result_text,
                process=True,
            )
            if name in {"command_exec", "process_input"}
            else _encode_tool_result(spec, result)
        )
        call.stage = "complete" if call.ok else "execution"
        return encoded

    def _mcp_tool(self, spec: ExposedTool) -> Tool:
        schema = spec.tool_class.model_json_schema(by_alias=True)
        policy.schema(self.config, spec.name, schema)
        return Tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            input_schema=schema,
            output_schema=(
                OUTPUTS[spec.name].model_json_schema() if spec.name in OUTPUTS else None
            ),
            annotations=spec.annotations,
        )

    def _with_runtime_defaults(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        return policy.arguments(self.config, name, arguments)


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


def result_target_path(arguments: dict[str, object]) -> Path | None:
    """Return a path-like audit target without reading or logging file contents."""
    value = arguments.get("path") or arguments.get("workdir")
    return Path(value) if isinstance(value, str) else None
