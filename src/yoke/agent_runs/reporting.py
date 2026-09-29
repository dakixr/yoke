"""Prompt-scoped reporting with registration admission and best-effort updates."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
import threading
import time

from yoke.agent_runs.context import Binding
from yoke.agent_runs.protocol import OPERATION_TIMEOUT, RegistrationError
from yoke.agent.models import TokenUsage

HEARTBEAT_SECONDS = 5.0
MAX_COUNT = 2**53 - 1
_ACTIVE_REPORTER: ContextVar[Reporter | None] = ContextVar(
    "yoke_agent_run_reporter", default=None
)


@contextmanager
def bind_reporter(reporter: Reporter) -> Iterator[None]:
    token = _ACTIVE_REPORTER.set(reporter)
    try:
        yield
    finally:
        _ACTIVE_REPORTER.reset(token)


def report_provider_usage(usage: TokenUsage | None) -> None:
    """Count each provider response, including compaction and retry calls."""
    reporter = _ACTIVE_REPORTER.get()
    if reporter is not None:
        reporter.usage(usage)


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
        self._started = time.monotonic()
        self._tool_uses = 0
        self._token_counts: dict[str, int] = {}
        self._unavailable_counts: set[str] = set()
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
            if self._token_counts or self._tool_uses or status != "running":
                frame["typedUsage"] = {
                    **self._token_counts,
                    "toolUses": self._tool_uses,
                    **(
                        {
                            "durationMs": min(
                                MAX_COUNT,
                                int((time.monotonic() - self._started) * 1000),
                            )
                        }
                        if status != "running"
                        else {}
                    ),
                }
            with suppress(Exception):
                self.binding.call(frame)

    def usage(self, usage: TokenUsage | None) -> None:
        values = {
            "inputTokens": getattr(usage, "input_tokens", None),
            "outputTokens": getattr(usage, "output_tokens", None),
            "cachedInputTokens": getattr(usage, "cached_input_tokens", None),
            "reasoningOutputTokens": getattr(usage, "reasoning_tokens", None),
        }
        total = getattr(usage, "total_tokens", None)
        if (
            total is None
            and type(values["inputTokens"]) is int
            and type(values["outputTokens"]) is int
        ):
            total = values["inputTokens"] + values["outputTokens"]
        values["totalTokens"] = total
        with self._lock:
            for key, value in values.items():
                if type(value) is not int or value < 0:
                    self._unavailable_counts.add(key)
                    self._token_counts.pop(key, None)
                elif key not in self._unavailable_counts:
                    self._token_counts[key] = min(
                        MAX_COUNT, self._token_counts.get(key, 0) + value
                    )
        self.report()

    def tool(self, name: object) -> None:
        if (
            isinstance(name, str)
            and 0 < len(name) <= 256
            and not any(ord(c) < 32 for c in name)
        ):
            with self._lock:
                self._last_tool = name
                self._tool_uses = min(MAX_COUNT, self._tool_uses + 1)
            self.report()

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.report()

    def finish(self, status: str, error_type: str | None = None) -> None:
        self._stop.set()
        self._thread.join(timeout=OPERATION_TIMEOUT + 0.1)
        self.report(status=status, error_type=error_type)
