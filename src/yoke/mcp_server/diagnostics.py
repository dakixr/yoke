"""Correlated MCP call outcomes, without logging request or result contents."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import uuid
from typing import Any

from pydantic import ValidationError

from yoke._version import __version__

logger = logging.getLogger(__name__)


def json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "number" if isinstance(value, (int, float)) else "other"


class MCPLogFormatter(logging.Formatter):
    """Write JSON records and retain only explicitly selected call metadata."""

    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(
                record.created, dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        value.update(getattr(record, "mcp_event", {}))
        # Exception strings/tracebacks can contain commands, tokens or file data.
        if record.exc_info and record.exc_info[0]:
            value["exception_type"] = record.exc_info[0].__name__
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(MCPLogFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler])


class CallDiagnostics:
    """Track one request through validation, execution and result encoding."""

    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        self.request_id = uuid.uuid4().hex
        self.name = name
        self.started = time.monotonic()
        self.stage = "input_validation"
        self.execution_started: bool | None = False
        self.ok = False
        self.outcome = "interrupted"
        self.error_code: str | None = None
        self.exception_type: str | None = None
        self.shape = {
            key: json_type(arguments[key])
            for key in ("cmd", "argv", "command")
            if name == "exec_command" and key in arguments
        }
        self.argument_count = len(arguments)
        self._log("tool_call_started")

    def begin_execution(self) -> None:
        self.stage = "execution"
        # Dispatch may fail before a process starts, or after partial work.
        self.execution_started = None

    def observe(self, result: dict[str, Any]) -> None:
        self.ok = bool(result.get("ok", True))
        process_result = isinstance(result.get("chunk_id"), str)
        if self.ok or process_result:
            self.execution_started = True
        self.outcome = "running" if result.get("running") else "succeeded"
        if not self.ok:
            self.outcome = "failed"
            self.error_code = "TOOL_ERROR"
            if result.get("error_code") == "OS_PERMISSION_DENIED":
                self.error_code = "OS_PERMISSION_DENIED"
            elif result.get("cancelled") or result.get("status") == "cancelled":
                self.error_code = "COMMAND_CANCELLED"
            elif result.get("timed_out"):
                self.error_code = "COMMAND_TIMEOUT"
            elif isinstance(result.get("exit_code"), int):
                self.error_code = "COMMAND_EXIT_NONZERO"
            result.update(
                request_id=self.request_id,
                stage=self.stage,
                execution_started=self.execution_started,
            )
            result.setdefault("error_code", self.error_code)

    def error(self, code: str, message: str) -> dict[str, Any]:
        self.ok = False
        self.outcome = "failed"
        self.error_code = code
        return {
            "ok": False,
            "error": message,
            "error_code": code,
            "stage": self.stage,
            "execution_started": self.execution_started,
            "request_id": self.request_id,
        }

    def invalid_arguments(self, exc: ValidationError) -> dict[str, Any]:
        self.stage = "input_validation"
        self.execution_started = False
        details = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "code": error["type"],
            }
            for error in exc.errors(
                include_input=False, include_context=False, include_url=False
            )
        ]
        message = "; ".join(f"{item['field']}: {item['message']}" for item in details)
        result = self.error("INVALID_ARGUMENT", "Invalid tool arguments: " + message)
        result["details"] = details
        result["recovery"] = (
            "Correct the arguments and retry. No tool execution started."
        )
        if self.name == "exec_command":
            result["recovery"] = (
                'Use exactly one of {"cmd":"pwd"} or {"argv":["pwd"]}. '
                "cmd must be a string, never an array. Correct the arguments and retry. "
                "No command started. This is not a permission denial."
            )
            if self.shape.get("cmd") == "array":
                result.update(field="cmd", expected="string", received="array")
        return result

    def finish(self) -> None:
        self._log("tool_call_finished")

    def _log(self, event: str) -> None:
        logger.info(
            "MCP tool request",
            extra={
                "mcp_event": {
                    "event": event,
                    "request_id": self.request_id,
                    "tool": self.name,
                    "version": __version__,
                    "pid": os.getpid(),
                    "argument_count": self.argument_count,
                    "argument_types": self.shape,
                    "stage": self.stage,
                    "execution_started": self.execution_started,
                    "ok": self.ok,
                    "outcome": self.outcome,
                    "error_code": self.error_code,
                    "exception_type": self.exception_type,
                    "duration_ms": round((time.monotonic() - self.started) * 1000),
                }
            },
        )
