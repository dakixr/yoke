"""Remote defaults and caps, shared by direct and composed dispatch."""

from typing import Any

from yoke.mcp_server.config import MCPServerConfig


def defaults(config: MCPServerConfig, name: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if name in {"command_exec", "python_exec", "process_input"}:
        values["max_output_tokens"] = config.max_output_tokens
    if name == "command_exec":
        values["login"] = False
    if name == "python_exec":
        values["timeout"] = config.python_timeout
    if name == "process_read":
        values["wait_ms"] = min(60_000, config.max_remote_wait_ms)
    return values


def arguments(
    config: MCPServerConfig, name: str, supplied: dict[str, Any]
) -> dict[str, Any]:
    values = {**defaults(config, name), **supplied}
    if name == "process_read":
        wait = values.get("wait_ms")
        if isinstance(wait, int) and not isinstance(wait, bool):
            values["wait_ms"] = min(wait, config.max_remote_wait_ms)
    # Keep unknown fields, including legacy execution wait fields, for
    # extra='forbid' validation.
    # process_input has a fixed 5000ms maximum, not the execution wait cap.
    return values


def schema(config: MCPServerConfig, name: str, value: dict[str, Any]) -> None:
    properties = value.get("properties", {})
    for key, default in defaults(config, name).items():
        if key in properties:
            properties[key]["default"] = default
    if name != "process_read":
        return
    wait = properties.get("wait_ms")
    if isinstance(wait, dict):
        wait["maximum"] = config.max_remote_wait_ms
        for variant in wait.get("anyOf", []):
            if variant.get("type") == "integer":
                variant["maximum"] = config.max_remote_wait_ms
