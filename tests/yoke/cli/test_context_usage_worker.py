from __future__ import annotations

import logging
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


def test_context_usage_worker_recovers_after_estimator_failure(caplog) -> None:
    next_published = Event()
    calls: list[str] = []
    state = PromptCliState(messages=[], pending_prompts=[])
    state.context_usage_text = "previous"
    state_lock = Lock()

    def estimate(text: str) -> str:
        calls.append(text)
        if text == "fails":
            raise RuntimeError("broken estimator")
        return text

    worker = ContextUsageWorker(
        state=state,
        state_lock=state_lock,
        estimate=estimate,
        invalidate=next_published.set,
    )
    caplog.set_level(
        logging.ERROR,
        logger="yoke.cli.interactive.prompt.context_usage",
    )

    worker.submit("fails")
    deadline = time.monotonic() + 2
    while "Failed to estimate context usage." not in caplog.text:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert state.context_usage_text == "previous"
    assert not next_published.is_set()

    worker.submit("next")

    assert next_published.wait(timeout=2)
    assert state.context_usage_text == "next"
    assert calls == ["fails", "next"]
