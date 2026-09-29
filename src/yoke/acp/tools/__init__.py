"""Project native tool events into self-contained, client-readable ACP updates."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from yoke.acp.tools.output import output_content

_FILE_KINDS = {
    "read": "read",
    "read_file": "read",
    "write": "edit",
    "edit": "edit",
    "apply_patch": "edit",
}
_PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M
)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _tool_call_id(value: str) -> str:
    # Only the registry observer owns agent notifications, never model tool IDs.
    return "yoke-tool:" + value if value.startswith("yoke-agent:") else value


def _label(value: str) -> str:
    single_line = " ".join(value.split())
    return single_line if len(single_line) <= 160 else single_line[:157] + "..."


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _arguments(value: Any) -> Any:
    # Native start events carry the model's JSON argument string; end events
    # carry a parsed mapping. Keep malformed input inspectable on failed calls.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def _process_ids(arguments: dict[str, Any]) -> str:
    sessions = arguments.get("sessions")
    values = (
        [item.get("session_id") for item in sessions if isinstance(item, dict)]
        if isinstance(sessions, list)
        else [arguments.get("session_id")]
    )
    return ", ".join(str(value) for value in values if type(value) is int)


def _metadata(name: str, raw_input: Any, cwd: str | None = None) -> dict[str, Any]:
    if name == "apply_patch" and isinstance(raw_input, str):
        # Native patch tools also accept a raw patch or a JSON string, not only
        # an object. Normalize only the display copy, as native parsing does.
        raw_input = {"input": raw_input}
    arguments = dict(raw_input) if isinstance(raw_input, dict) else {}
    title = name or "Tool"
    kind = "other"
    paths: list[str] = []
    if name in _FILE_KINDS:
        kind = _FILE_KINDS[name]
        if name == "edit" and arguments.get("delete_file") is True:
            kind = "delete"
        path = _text(arguments.get("path"))
        paths = [path] if path else []
        if name == "apply_patch":
            paths = list(
                dict.fromkeys(_PATCH_PATH.findall(_text(arguments.get("input"))))
            )
            if paths:
                arguments.setdefault("path", paths[0])
        verb = {"read": "Read file", "edit": "Change files", "delete": "Delete file"}[
            kind
        ]
        title = f"{verb} · {paths[0]}" if paths else verb
    elif name in ("command_exec", "python_exec"):
        kind = "execute"
        if name == "python_exec":
            # Keep the script in code rather than duplicating it in the label.
            command = _text(arguments.get("python_executable")) or "python"
        else:
            command = _text(arguments.get("cmd")) or _text(arguments.get("command"))
            if not command and (argv := _strings(arguments.get("argv"))):
                command = shlex.join(argv)
        if command:
            # ACP-only display aliases let stock clients recognize Yoke's
            # cmd/argv and patterns without changing native/provider inputs.
            arguments.setdefault("command", command)
        title = f"Run · {command}" if command else "Run command"
    elif name in ("rg", "fd"):
        kind = "search"
        query = _text(arguments.get("pattern")) or " | ".join(
            _strings(arguments.get("patterns"))
        )
        if not query:
            query = ", ".join(_strings(arguments.get("paths"))) or "files"
        arguments.setdefault("query", query)
        title = f"Search files · {query}"
    elif name in ("process_read", "process_input", "process_cancel"):
        verb = {
            "process_read": "Read process output"
            if arguments.get("wait_ms") == 0
            else "Monitoring processes",
            "process_input": "Send process input",
            "process_cancel": "Stop process",
        }[name]
        ids = _process_ids(arguments)
        title = f"{verb} · {ids}" if ids else verb
    elif name == "mcp_call":
        target = ".".join(
            value for key in ("server", "tool") if (value := _text(arguments.get(key)))
        )
        title = target or "Call MCP tool"
    elif name == "mcp_inspect":
        server = _text(arguments.get("server"))
        title = f"Inspect MCP tools · {server}" if server else "Inspect MCP tools"
    elif name == "skill":
        skills = ", ".join(_strings(arguments.get("load")))
        title = f"Load skills · {skills}" if skills else "List skills"

    result: dict[str, Any] = {
        "title": _label(title),
        "kind": kind,
        "rawInput": arguments if isinstance(raw_input, dict) else raw_input,
    }
    # Only use the native session's workspace, never the ACP process cwd.
    # Without that identity, relative paths remain labels, not file locations.
    locations: list[dict[str, Any]] = []
    for path in paths[:8]:
        if not (
            PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
        ):
            if not cwd or not PurePosixPath(cwd).is_absolute():
                continue
            path = posixpath.normpath(posixpath.join(cwd, path))
        location: dict[str, Any] = {"path": path}
        offset = arguments.get("offset")
        if kind == "read" and type(offset) is int and 0 < offset <= 0xFFFFFFFF:
            location["line"] = offset
        locations.append(location)
    # An empty list also clears a pre-hook target on a completion update.
    result["locations"] = locations
    return result


class ToolCallProjector:
    """Retain only in-flight display metadata, scoped to one ACP prompt."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd
        self._pending: dict[str, dict[str, Any]] = {}
        self._completed: set[str] = set()

    def has_started(self, call_id: str) -> bool:
        return call_id in self._pending or call_id in self._completed

    def has_completed(self, call_id: str) -> bool:
        return call_id in self._completed

    def update(self, *, start: bool, data: dict[str, Any]) -> dict[str, Any]:
        call_id = data["tool_call_id"]
        name = _text(data.get("tool_name"))
        if start:
            metadata = _metadata(name, _arguments(data.get("tool_arguments")), self.cwd)
            self._pending[call_id] = metadata
        else:
            self._completed.add(call_id)
            previous = self._pending.pop(call_id, None)
            executed = data.get("executed_arguments")
            metadata = (
                _metadata(name, _arguments(executed), self.cwd)
                if name and executed is not None
                else previous or _metadata(name, None, self.cwd)
            )
        # Repeat identity on completion. Some clients derive presentation from
        # each delta before merging it, otherwise output replaces the row label.
        update = {
            **metadata,
            "sessionUpdate": "tool_call" if start else "tool_call_update",
            "toolCallId": _tool_call_id(call_id),
            "status": "in_progress"
            if start
            else ("completed" if data["ok"] else "failed"),
        }
        if not start:
            result = data.get("result")
            update["rawOutput"] = (
                {"toolResult": result}
                if isinstance(result, dict) and result.get("type") == "yoke_agent"
                else result
            )
            update["content"] = output_content(result)
        return update

    def finish(self, *, interrupted: bool) -> list[dict[str, Any]]:
        """Close unconfirmed calls without inventing a successful native result."""
        message = (
            "Tool call interrupted; no final result was received."
            if interrupted
            else "Tool result unavailable after the turn ended."
        )
        updates = [
            {
                **metadata,
                "sessionUpdate": "tool_call_update",
                "toolCallId": _tool_call_id(call_id),
                "status": "failed",
                "content": output_content(message),
            }
            for call_id, metadata in self._pending.items()
        ]
        self._pending.clear()
        return updates
