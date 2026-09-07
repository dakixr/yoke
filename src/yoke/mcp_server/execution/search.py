"""Cancellable searches with a hard capture limit for composed MCP reads."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import threading
import time
from typing import Any

from yoke.mcp_server.search import (
    MCPFdTool,
    MCPRipgrepTool,
)


def execute(
    tool: MCPFdTool | MCPRipgrepTool, cancel: threading.Event | None
) -> dict[str, Any]:
    rg = isinstance(tool, MCPRipgrepTool)
    binary = shutil.which("rg" if rg else "fd")
    if binary is None:
        raise ValueError("Search executable not found")
    root = tool._resolve_search_root()
    command = tool._build_command(binary, root)
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
        effective_code = 0 if truncated and code not in {0, 1} else code
        result = tool._render_output(stdout, stderr, command, effective_code)
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
