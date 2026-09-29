"""Bounded JSON frames for the private, local run-reporting channel."""

from __future__ import annotations

import json
import os
import socket
import struct
import time

ENVIRONMENT_KEY = "YOKE_AGENT_RUN_HOST"
MAX_FRAME_BYTES = 16_384
OPERATION_TIMEOUT = 0.75


class RegistrationError(RuntimeError):
    """The managing host did not acknowledge a run registration."""


def receive(sock: socket.socket, deadline: float) -> dict[str, object]:
    def read(count: int) -> bytes:
        parts = bytearray()
        while len(parts) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Run reporting deadline expired")
            sock.settimeout(remaining)
            part = sock.recv(count - len(parts))
            if not part:
                raise EOFError("Run reporting connection closed")
            parts.extend(part)
        return bytes(parts)

    length = struct.unpack("!I", read(4))[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise ValueError("Invalid run reporting frame size")
    result = json.loads(read(length))
    if not isinstance(result, dict):
        raise ValueError("Invalid run reporting frame")
    return result


def send(sock: socket.socket, value: dict[str, object], deadline: float) -> None:
    raw = json.dumps(value, separators=(",", ":")).encode()
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("Run reporting frame too large")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Run reporting deadline expired")
    sock.settimeout(remaining)
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def request(address: str, frame: dict[str, object]) -> dict[str, object]:
    deadline = time.monotonic() + OPERATION_TIMEOUT
    try:
        local_tcp = address.startswith("tcp:")
        family = socket.AF_INET if local_tcp else socket.AF_UNIX
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(OPERATION_TIMEOUT)
            sock.connect(("127.0.0.1", int(address[4:])) if local_tcp else address)
            send(sock, {**frame, "pid": os.getpid()}, deadline)
            result = receive(sock, deadline)
        if result.get("ok") is not True:
            raise RegistrationError("Agent run host denied reporting")
        return result
    except (OSError, EOFError, ValueError) as exc:
        raise RegistrationError("Agent run host is unavailable") from exc
