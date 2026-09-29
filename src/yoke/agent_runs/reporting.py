"""Prompt-scoped reporting with registration admission and best-effort updates."""

from __future__ import annotations

from contextlib import suppress
import threading

from yoke.agent_runs.context import Binding
from yoke.agent_runs.protocol import OPERATION_TIMEOUT, RegistrationError

HEARTBEAT_SECONDS = 5.0


class Reporter:
    def __init__(self, host: Binding, metadata: dict[str, object]) -> None:
        # A single retry is safe when the first ACK was lost. Registration is idempotent.
        try:
            response = host.call({"op": "register", **metadata})
        except RegistrationError:
            response = host.call({"op": "register", **metadata})
        token = response.get("token")
        if not isinstance(token, str) or not token or len(token) > 256:
            raise RegistrationError("Agent run host did not acknowledge registration")
        self.binding = Binding(token=token, address=host.endpoint())
        self._lock = threading.Lock()
        self._sequence = 0
        self._stop = threading.Event()
        self._last_tool: str | None = None
        self._thread = threading.Thread(
            target=self._heartbeat, daemon=True, name="yoke-agent-run-heartbeat"
        )
        self._thread.start()

    def report(self, *, status: str = "running", error_type: str | None = None) -> None:
        with self._lock:
            self._sequence += 1
            frame: dict[str, object] = {
                "op": "report",
                "sequence": self._sequence,
                "status": status,
                "errorType": error_type,
                "lastToolName": self._last_tool,
            }
            with suppress(Exception):
                self.binding.call(frame)

    def tool(self, name: object) -> None:
        if (
            isinstance(name, str)
            and 0 < len(name) <= 256
            and not any(ord(c) < 32 for c in name)
        ):
            self._last_tool = name
            self.report()

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.report()

    def finish(self, status: str, error_type: str | None = None) -> None:
        self._stop.set()
        self._thread.join(timeout=OPERATION_TIMEOUT + 0.1)
        self.report(status=status, error_type=error_type)
