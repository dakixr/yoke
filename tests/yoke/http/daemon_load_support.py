"""Scripted native agents for HTTP daemon concurrency and load tests.

The daemon gets a real ``RuntimeAgent`` per session. Only the provider is
scripted, and the only tool is an in-process tool that blocks on events the
test controls, so stop, steer and slot accounting run through production code.
"""

# ruff: noqa: D101, D102, D103, S101
from __future__ import annotations

import json
import secrets
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools import LocalTool
from yoke.ai.providers.base import Provider
from yoke.session import SessionRecord

TOKEN = "daemon-load-secret"
API = "/api/v1"


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class Controller:
    """Shared test-side state for every agent the daemon builds."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.release: defaultdict[str, Event] = defaultdict(Event)
        self.started: defaultdict[str, Event] = defaultdict(Event)
        self.running: set[str] = set()
        self.max_parallel = 0
        self.tool_runs: list[str] = []

    def enter(self, key: str) -> None:
        with self.lock:
            self.tool_runs.append(key)
            self.running.add(key)
            self.max_parallel = max(self.max_parallel, len(self.running))
        self.started[key].set()

    def leave(self, key: str) -> None:
        with self.lock:
            self.running.discard(key)

    def factory(self, record: SessionRecord) -> RuntimeAgent:
        assert record.root is not None
        return RuntimeAgent(
            ScriptedProvider(self),
            [WaitTool.bind(controller=self)],
            tool_root=Path(record.root),
        )


class WaitTool(LocalTool):
    """Block on a named event. ``cooperative`` decides whether stop is honored."""

    name = "wait_for"
    description = "Block until the test releases the named event."
    execute_in_process = True
    key: str
    cooperative: bool = True

    def execute(self) -> dict[str, object]:
        controller = self._context["controller"]
        assert isinstance(controller, Controller)
        controller.enter(self.key)
        try:
            release = controller.release[self.key]
            deadline = time.monotonic() + 15
            while not release.wait(0.01):
                if self.cooperative and self._is_cancel_requested():
                    return {"ok": False, "cancelled": True}
                if time.monotonic() > deadline:
                    return {"ok": False, "error": "test never released the tool"}
            return {"ok": True, "key": self.key}
        finally:
            controller.leave(self.key)


def latest_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.plain_text_content or ""
    return ""


class ScriptedProvider(Provider):
    """Script turns from the newest user prompt.

    ``block:KEY`` calls a cooperative tool, ``hang:KEY`` a tool that ignores
    stop, and ``stall:KEY`` blocks inside the provider call itself, which keeps
    the turn's worker thread even after the turn is retired. Anything else is
    answered immediately.
    """

    supports_image_inputs = True

    def __init__(self, controller: Controller) -> None:
        self.controller = controller

    def fork_for_turn(self) -> ScriptedProvider:
        return ScriptedProvider(self.controller)

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        prompt = latest_user_text(messages)
        if messages[-1].role == "tool":
            return Message.assistant(f"done:{prompt}")
        mode, _, key = prompt.partition(":")
        if mode == "stall":
            self.controller.enter(key)
            try:
                assert self.controller.release[key].wait(15)
            finally:
                self.controller.leave(key)
            return Message.assistant(f"done:{prompt}")
        if mode not in {"block", "hang"}:
            return Message.assistant(f"done:{prompt}")
        arguments = {"key": key, "cooperative": mode == "block"}
        return Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"call-{secrets.token_hex(4)}",
                    function=ToolFunction(
                        name="wait_for", arguments=json.dumps(arguments)
                    ),
                )
            ],
        )


def create_session(client: TestClient, root: Path, session_id: str) -> None:
    response = client.post(
        f"{API}/session",
        headers=auth(),
        json={
            "id": session_id,
            "title": "Load session",
            "location": {"directory": str(root)},
        },
    )
    assert response.status_code == 200, response.text


def prompt(
    client: TestClient,
    session_id: str,
    text: str,
    *,
    delivery: str = "steer",
    resume: bool = True,
) -> dict:
    response = client.post(
        f"{API}/session/{session_id}/prompt",
        headers=auth(),
        json={"prompt": {"text": text}, "delivery": delivery, "resume": resume},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def wait_state(client: TestClient, session_id: str, timeout_ms: int = 5000) -> str:
    response = client.post(
        f"{API}/session/{session_id}/wait",
        headers=auth(),
        params={"timeoutMs": timeout_ms},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["state"]


def message_texts(client: TestClient, session_id: str) -> list[str]:
    response = client.get(
        f"{API}/session/{session_id}/message",
        headers=auth(),
        params={"order": "asc", "limit": 200},
    )
    assert response.status_code == 200, response.text
    return [
        part["text"]
        for message in response.json()["data"]
        for part in message.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    ]


def timed[T](action: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    result = action()
    return result, time.perf_counter() - started


def percentiles(samples: list[float]) -> tuple[float, float]:
    """Return p50 and p95 in milliseconds."""
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return statistics.median(ordered) * 1000, p95 * 1000


class LiveServer:
    """Serve one app on a loopback socket from a thread, as ``yoke serve`` does."""

    def __init__(self, app: FastAPI) -> None:
        import socket

        import uvicorn

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.port = int(self.socket.getsockname()[1])
        self.server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False)
        )
        self.thread = Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )

    def __enter__(self) -> LiveServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert self.thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.socket.close()
        assert not self.thread.is_alive()


class RawSseReader:
    """An SSE client with a tiny receive buffer that reads only when told to."""

    def __init__(self, port: int) -> None:
        import socket

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.socket.settimeout(10)
        self.socket.connect(("127.0.0.1", port))
        self.socket.sendall(
            (
                f"GET {API}/event HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {TOKEN}\r\nAccept: text/event-stream\r\n\r\n"
            ).encode()
        )
        self.buffer = b""
        self.closed = False

    def next_event(self) -> dict | None:
        """Return the next SSE data payload, or None once the server closes."""
        while True:
            separator = self.buffer.find(b"\n\n")
            if separator >= 0:
                block, self.buffer = (
                    self.buffer[:separator],
                    self.buffer[separator + 2 :],
                )
                for line in block.split(b"\n"):
                    line = line.rstrip(b"\r")
                    if line.startswith(b"data: "):
                        return json.loads(line[6:])
                continue
            if self.closed:
                return None
            chunk = self.socket.recv(65536)
            if not chunk:
                self.closed = True
                continue
            self.buffer += self._dechunk(chunk)

    def _dechunk(self, data: bytes) -> bytes:
        # Strip HTTP headers and chunked framing lines; SSE payload lines are JSON
        # and never consist of a bare hexadecimal chunk size.
        self._raw = getattr(self, "_raw", b"") + data
        if not getattr(self, "_headers_done", False):
            end = self._raw.find(b"\r\n\r\n")
            if end < 0:
                return b""
            self._headers_done = True
            self._raw = self._raw[end + 4 :]
        out = b""
        while True:
            line_end = self._raw.find(b"\r\n")
            if line_end < 0:
                return out
            size = int(self._raw[:line_end].split(b";")[0], 16)
            if size == 0:
                self.closed = True
                self._raw = b""
                return out
            if len(self._raw) < line_end + 2 + size + 2:
                return out
            out += self._raw[line_end + 2 : line_end + 2 + size]
            self._raw = self._raw[line_end + 2 + size + 2 :]

    def close(self) -> None:
        self.socket.close()
