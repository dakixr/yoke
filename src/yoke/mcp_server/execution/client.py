"""Helper API injected as yoke_mcp into managed Python subprocesses."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

_address = ""
_token = ""


def configure(address: str, token: str) -> None:
    global _address, _token
    _address, _token = address, token


def _request(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = (
        json.dumps(
            {"token": _token, "operation": operation, "arguments": arguments}
        ).encode()
        + b"\n"
    )
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("Bridge request exceeds 4 MiB")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(180)
        connection.connect(_address)
        connection.sendall(payload)
        with connection.makefile("rb") as reader:
            raw = reader.readline(8 * 1024 * 1024 + 1)
    result = json.loads(raw)
    if result.get("bridge_error"):
        raise RuntimeError(result["bridge_error"])
    return result


class Result:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.data = payload
        self.status = "ok" if payload.get("ok", True) else "error"
        self.error = payload.get("error")


class Tools:
    async def call(self, name: str, arguments: dict[str, Any]) -> Result:
        return Result(
            await asyncio.to_thread(
                _request, "call", {"name": name, "arguments": arguments}
            )
        )

    async def mcp(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        schema_hash: str | None = None,
    ) -> Result:
        return Result(
            await asyncio.to_thread(
                _request,
                "mcp",
                {
                    "server": server,
                    "tool": tool,
                    "arguments": arguments,
                    "schema_hash": schema_hash,
                },
            )
        )

    async def gather(self, calls: list[Any]) -> list[Result]:
        return list(await asyncio.gather(*calls))


class Output:
    def emit(self, value: Any) -> dict[str, Any]:
        result = _request("emit", {"data": value})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return result


tools = Tools()
output = Output()
