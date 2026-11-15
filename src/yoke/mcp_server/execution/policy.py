"""Remote defaults and caps, shared by direct and composed dispatch."""

from typing import Any

from yoke.mcp_server.config import MCPServerConfig

_BUDGET_KINDS = {
    "python_exec": "python",
    "result_read": "result",
    "process_read": "process",
    "export_file": "export",
}


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


def budget(config: MCPServerConfig, name: str, arguments: dict[str, Any]) -> int:
    """Select the bounded response budget for one execution action."""
    raw = arguments.get("max_output_tokens")
    if name == "python_exec" and raw is None:
        raw = config.max_output_tokens
    if name == "result_read":
        return 150_000
    if name == "process_read":
        return 400_000
    if name == "export_file":
        return 3 * 1024 * 1024
    return min(64_000, raw * 4) if isinstance(raw, int) else 32_000


def encoding_flags(name: str) -> tuple[bool, bool, bool]:
    """Return (process, batch, process_recipe) for result encoding."""
    return (
        name in {"python_exec", "process_read"},
        name == "batch_read",
        name == "check_patch",
    )
