"""Cancellable searches with a hard capture limit for composed MCP reads."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import threading
import time
from typing import Any

from yoke.mcp_server.search import MCPFdTool, MCPRipgrepTool, _is_fd_execution_argument


def execute(
    tool: MCPFdTool | MCPRipgrepTool, cancel: threading.Event | None
) -> dict[str, Any]:
    arguments = tool._parse_raw_args()
    rg = isinstance(tool, MCPRipgrepTool)
    if rg and any(arg == "--pre" or arg.startswith("--pre=") for arg in arguments):
        raise ValueError("rg --pre is disabled in read-only operations")
    if not rg and any(_is_fd_execution_argument(arg) for arg in arguments):
        raise ValueError("fd execution is disabled in read-only operations")
    binary = shutil.which("rg" if rg else "fd")
    if binary is None:
        raise ValueError("Search executable not found")
    root = tool._resolve_search_root()
    command = [binary]
    if rg:
        command += ["--no-config"]
        if "--json" not in arguments:
            command.append("--json")
    command += arguments
    if rg and not tool._has_explicit_path(arguments):
        command.append(str(root))
    process = subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    buffers = {"out": bytearray(), "err": bytearray()}
    truncated = False
    deadline = time.monotonic() + 20
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                if (cancel and cancel.is_set()) or time.monotonic() >= deadline:
                    return {
                        "ok": False,
                        "status": "cancelled"
                        if cancel and cancel.is_set()
                        else "error",
                        "error": "Search cancelled or timed out",
                    }
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(data)
                    if sum(len(b) for b in buffers.values()) >= 4 * 1024 * 1024:
                        truncated = True
                        break
                if truncated:
                    break
        if truncated:
            process.terminate()
        code = process.wait(timeout=2)
        stdout, stderr = (
            buffers[key].decode("utf-8", errors="replace") for key in ("out", "err")
        )
        if rg:
            result = tool._parse_json_output(stdout, command)
            if result is None:
                result = tool._render_text(stdout, stderr)
        else:
            result = tool._render_output(stdout, stderr, command, code)
        if code not in {0, 1} and not truncated:
            return {"ok": False, "error": stderr[:4000], "exit_code": code}
        if truncated:
            result["truncated"] = True
        result["complete"] = not result.get("truncated", False)
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
