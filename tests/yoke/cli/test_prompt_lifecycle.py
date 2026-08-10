from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Event, Lock, Thread, get_ident
import time
from typing import Any, cast
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.lifecycle import PromptLifecycleConfig
from yoke.cli.interactive.prompt.lifecycle import run_persistent_prompt_application


@dataclass
class _Size:
    columns: int = 80


class _Output:
    def get_size(self) -> _Size:
        return _Size()


class _Buffer:
    def __init__(self) -> None:
        self.text = "first prompt"
        self.accept_handler = None
        self.document = None

    def set_document(self, document: object) -> None:
        self.document = document


class _App:
    def __init__(self, buffer: _Buffer) -> None:
        self.buffer = buffer
        self.output = _Output()
        self.loop = None
        self.min_redraw_interval = None
        self.refresh_interval = 5.0
        self.run_count = 0
        self.invalidate_count = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._result: asyncio.Future[str] | None = None

    def create_background_task(self, coroutine) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.append(task)
        return task

    def invalidate(self) -> None:
        self.invalidate_count += 1

    def exit(self, *, result: str) -> None:
        assert self._result is not None
        self._result.set_result(result)

    def run(self, *, pre_run) -> str:
        self.run_count += 1

        async def drive() -> str:
            self.loop = asyncio.get_running_loop()
            self._result = self.loop.create_future()
            pre_run()
            assert self.buffer.accept_handler is not None
            assert self.buffer.accept_handler(self.buffer) is False
            self.buffer.text = "must not run after exit"
            assert self.buffer.accept_handler(self.buffer) is False
            result = await asyncio.wait_for(self._result, timeout=2)
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            return result

        return asyncio.run(drive())


class _Session:
    def __init__(self, *, app_type: type[_App] = _App) -> None:
        self.default_buffer = _Buffer()
        self.app = app_type(self.default_buffer)
        self.message = ""
        self.bottom_toolbar = None
        self.key_bindings = None
        self.completer = None
        self.complete_while_typing = False
        self.multiline = False
        self.reserve_space_for_menu = 0
        self.style = None


class _Scrollback:
    def __init__(self) -> None:
        self.closed = False
        self.flushed = False

    async def run(self, _app: object) -> None:
        while not self.closed:
            await asyncio.sleep(0.01)

    async def flush(self, _app: object) -> None:
        self.flushed = True

    def emit(self, *_args: object, **_kwargs: object) -> None:
        return

    def close(self) -> None:
        self.closed = True

    def drain_sync(self, *, width: int) -> None:
        del width


def test_one_application_serializes_submission_off_ui_loop() -> None:
    state = PromptCliState(messages=[], pending_prompts=[])
    state_lock = Lock()
    session = _Session()
    scrollback = _Scrollback()
    processed: list[tuple[str, str, int]] = []
    persisted: list[None] = []
    ui_thread = get_ident()

    def process(prompt: str, action: str) -> None:
        processed.append((prompt, action, get_ident()))
        with state_lock:
            state.shutdown_requested = True

    result = run_persistent_prompt_application(
        PromptLifecycleConfig(
            state=state,
            state_lock=state_lock,
            prompt_session=cast(Any, session),
            completer=cast(Any, None),
            key_bindings=cast(Any, None),
            bottom_toolbar=lambda: [],
            scrollback=cast(Any, scrollback),
            process_submission=process,
            persist_exit=lambda: persisted.append(None),
            request_exit=lambda: None,
        )
    )

    assert result == 0
    assert session.app.run_count == 1
    assert [(prompt, action) for prompt, action, _thread in processed] == [
        ("first prompt", "steer")
    ]
    assert processed[0][2] != ui_thread
    assert persisted == [None]
    assert scrollback.flushed and scrollback.closed
    assert session.app.refresh_interval is None


class _AbruptApp(_App):
    def run(self, *, pre_run) -> str:
        self.run_count += 1

        async def drive() -> None:
            self.loop = asyncio.get_running_loop()
            pre_run()
            assert self.buffer.accept_handler is not None
            assert self.buffer.accept_handler(self.buffer) is False
            await asyncio.sleep(0.02)
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        asyncio.run(drive())
        raise EOFError


class _PersistenceRaceApp(_AbruptApp):
    def run(self, *, pre_run) -> str:
        self.run_count += 1

        async def drive() -> None:
            self.loop = asyncio.get_running_loop()
            pre_run()
            assert self.buffer.accept_handler is not None
            assert self.buffer.accept_handler(self.buffer) is False
            await asyncio.sleep(0.08)
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        asyncio.run(drive())
        raise EOFError


def test_abrupt_exit_waits_for_submission_before_persisting() -> None:
    state = PromptCliState(messages=[], pending_prompts=[])
    state_lock = Lock()
    session = _Session(app_type=_AbruptApp)
    scrollback = _Scrollback()
    started = Event()
    release = Event()
    events: list[str] = []
    exit_requests: list[None] = []

    def process(_prompt: str, _action: str) -> None:
        started.set()
        release.wait(timeout=2)
        events.append("submission finished")

    def release_submission() -> None:
        assert started.wait(timeout=1)
        time.sleep(0.05)
        release.set()

    Thread(target=release_submission, daemon=True).start()
    result = run_persistent_prompt_application(
        PromptLifecycleConfig(
            state=state,
            state_lock=state_lock,
            prompt_session=cast(Any, session),
            completer=cast(Any, None),
            key_bindings=cast(Any, None),
            bottom_toolbar=lambda: [],
            scrollback=cast(Any, scrollback),
            process_submission=process,
            persist_exit=lambda: events.append("persisted"),
            request_exit=lambda: exit_requests.append(None),
        )
    )

    assert result == 0
    assert events == ["submission finished", "persisted"]
    assert exit_requests == [None, None]


def test_abrupt_exit_joins_inflight_persistence_without_duplicate_write() -> None:
    state = PromptCliState(messages=[], pending_prompts=[])
    state_lock = Lock()
    session = _Session(app_type=_PersistenceRaceApp)
    scrollback = _Scrollback()
    persistence_started = Event()
    release_persistence = Event()
    persistence_calls: list[None] = []

    def process(_prompt: str, _action: str) -> None:
        with state_lock:
            state.shutdown_requested = True

    def persist() -> None:
        persistence_calls.append(None)
        persistence_started.set()
        release_persistence.wait(timeout=2)

    def release() -> None:
        assert persistence_started.wait(timeout=1)
        time.sleep(0.1)
        release_persistence.set()

    Thread(target=release, daemon=True).start()
    result = run_persistent_prompt_application(
        PromptLifecycleConfig(
            state=state,
            state_lock=state_lock,
            prompt_session=cast(Any, session),
            completer=cast(Any, None),
            key_bindings=cast(Any, None),
            bottom_toolbar=lambda: [],
            scrollback=cast(Any, scrollback),
            process_submission=process,
            persist_exit=persist,
            request_exit=lambda: None,
        )
    )

    assert result == 0
    assert persistence_calls == [None]
