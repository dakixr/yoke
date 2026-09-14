"""MCP command arguments, without changing the agent's command contract."""

from __future__ import annotations

from copy import deepcopy
import sys
from typing import Any

from pydantic import ConfigDict

from yoke.agent.tools.command import ExecCommandTool


def _command_schema(schema: dict[str, Any]) -> None:
    # Some MCP clients lose named fields when projecting a root-level union.
    # Keep a plain object. The runtime still enforces exactly one command mode.
    schema.pop("anyOf", None)
    properties = schema["properties"]
    properties["command"] = {
        **deepcopy(properties["cmd"]),
        "description": "Legacy alias for cmd. Do not combine with cmd or argv.",
        "deprecated": True,
    }
    for variant in properties["argv"]["anyOf"]:
        if variant.get("type") == "array":
            variant["items"]["minLength"] = 1


class MCPExecCommandTool(ExecCommandTool):
    """Use cmd for shell text or argv for direct arguments, never both."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_command_schema)

    def _error(self, error: str, **payload: object) -> dict[str, object]:
        result = super()._error(error, **payload)
        # The shared executor invokes this hook inside its exception handler.
        # Preserve the OS exception category, without parsing its message or
        # changing the ordinary agent's error payload.
        if isinstance(sys.exception(), PermissionError):
            result["error_code"] = "OS_PERMISSION_DENIED"
        return result
