from __future__ import annotations

from threading import Event, Lock
import time

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.context_usage import ContextUsageWorker


def test_context_usage_worker_coalesces_pending_estimates() -> None:
    first_started = Event()
    release_first = Event()
    calls: list[str] = []
    active = 0
    maximum_active = 0
    state = PromptCliState(messages=[], pending_prompts=[])
    state_lock = Lock()

    def estimate(text: str) -> str:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        calls.append(text)
        if text == "first":
            first_started.set()
            release_first.wait(timeout=2)
        active -= 1
        return text

    worker = ContextUsageWorker(
        state=state,
        state_lock=state_lock,
        estimate=estimate,
        invalidate=lambda: None,
    )
    worker.submit("first")
    assert first_started.wait(timeout=2)
    worker.submit("obsolete")
    worker.submit("latest")
    release_first.set()

    deadline = time.monotonic() + 2
    while state.context_usage_text != "latest" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert state.context_usage_text == "latest"
    assert calls == ["first", "latest"]
    assert maximum_active == 1
