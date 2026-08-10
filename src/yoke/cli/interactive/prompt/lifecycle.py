"""Persistent prompt-toolkit application lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.completion.menu import COMPLETION_MENU_STYLE
from yoke.cli.interactive.prompt.scrollback import BatchedScrollback

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer
    from prompt_toolkit.key_binding import KeyBindingsBase


@dataclass(frozen=True, slots=True)
class PromptLifecycleConfig:
    """Dependencies owned by one long-lived prompt application."""

    state: PromptCliState
    state_lock: Lock
    prompt_session: PromptSession[str]
    completer: Completer
    key_bindings: KeyBindingsBase
    bottom_toolbar: Callable[[], object]
    scrollback: BatchedScrollback
    process_submission: Callable[[str, str], None]
    persist_exit: Callable[[], None]
    request_exit: Callable[[], None]


@dataclass(frozen=True, slots=True)
class _Submission:
    prompt: str
    action: str


class PersistentPromptLifecycle:
    """Keep prompt editing alive while work and output happen elsewhere."""

    def __init__(self, config: PromptLifecycleConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[_Submission] | None = None
        self._submission_active = False
        self._submission_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="yoke-prompt-submit",
        )
        self._submission_future: Future[None] | None = None
        self._persistence_future: Future[None] | None = None
        self._exit_persisted = False
        self._animation_task: asyncio.Task[None] | None = None

    def run(self) -> int:
        """Run exactly one prompt-toolkit application instance."""
        self._configure_session()
        app = self._config.prompt_session.app
        try:
            result = app.run(pre_run=self._start_background_tasks)
        except (EOFError, KeyboardInterrupt):
            self._config.request_exit()
            self._wait_for_active_submission()
            # A callback already past its shutdown check may have started a turn.
            self._config.request_exit()
            self._wait_for_exit_persistence()
            self._persist_after_abrupt_exit()
            return 0
        finally:
            self._config.scrollback.close()
            self._submission_executor.shutdown(wait=True, cancel_futures=True)
        del result
        return 0

    def _configure_session(self) -> None:
        session = self._config.prompt_session
        session.message = "› "
        session.bottom_toolbar = self._config.bottom_toolbar
        session.key_bindings = self._config.key_bindings
        session.completer = self._config.completer
        session.complete_while_typing = True
        session.multiline = True
        session.reserve_space_for_menu = 6
        session.style = COMPLETION_MENU_STYLE
        session.app.min_redraw_interval = 0.05
        session.app.refresh_interval = None
        session.default_buffer.accept_handler = self._accept_submission

    def _start_background_tasks(self) -> None:
        app = self._config.prompt_session.app
        self._queue = asyncio.Queue()
        app.create_background_task(self._config.scrollback.run(app))
        app.create_background_task(self._consume_submissions())
        app.create_background_task(self._watch_shutdown())
        self._ensure_animation()

    def _accept_submission(self, buffer: Any) -> bool:
        queue = self._queue
        if queue is None:
            return True
        with self._config.state_lock:
            action = self._config.state.submit_action
            self._config.state.submit_action = "steer"
            self._config.state.editor_revision += 1
        prompt = _normalize_submitted_prompt(buffer.text)
        queue.put_nowait(_Submission(prompt, action))
        return False

    async def _consume_submissions(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            submission = await queue.get()
            with self._config.state_lock:
                shutting_down = self._config.state.shutdown_requested
            if shutting_down:
                queue.task_done()
                continue
            self._submission_active = True
            try:
                await self._process_submission(submission)
            except Exception as exc:  # keep the editor alive after command failures
                self._config.scrollback.emit("error", str(exc))
            finally:
                self._submission_active = False
                queue.task_done()
                self._restore_editor_text()
                self._ensure_animation()
                self._config.prompt_session.app.invalidate()

    async def _process_submission(self, submission: _Submission) -> None:
        def callback() -> None:
            self._config.process_submission(
                submission.prompt,
                submission.action,
            )

        if _requires_terminal_control(submission.prompt):
            from prompt_toolkit.application.run_in_terminal import run_in_terminal

            await run_in_terminal(callback, in_executor=True)
            return
        future = self._submission_executor.submit(callback)
        self._submission_future = future
        try:
            await asyncio.wrap_future(future)
        finally:
            if future.done():
                self._submission_future = None

    def _wait_for_active_submission(self) -> None:
        future = self._submission_future
        if future is not None:
            try:
                future.result()
            except Exception as exc:
                self._config.scrollback.emit("error", str(exc))

    def _wait_for_exit_persistence(self) -> None:
        future = self._persistence_future
        if future is None:
            return
        try:
            future.result()
        except Exception as exc:
            self._config.scrollback.emit("error", str(exc))
        else:
            self._exit_persisted = True

    def _restore_editor_text(self) -> None:
        with self._config.state_lock:
            text = self._config.state.next_editor_text
            self._config.state.next_editor_text = None
        if text is None:
            return
        from prompt_toolkit.document import Document

        buffer = self._config.prompt_session.default_buffer
        buffer.set_document(Document(text, cursor_position=len(text)))

    def _ensure_animation(self) -> None:
        if self._animation_task is not None and not self._animation_task.done():
            return
        with self._config.state_lock:
            active = self._config.state.worker is not None
        if active:
            self._animation_task = (
                self._config.prompt_session.app.create_background_task(
                    self._animate_active_turn()
                )
            )

    async def _animate_active_turn(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            with self._config.state_lock:
                active = self._config.state.worker is not None
            if not active:
                return
            self._config.prompt_session.app.invalidate()

    async def _watch_shutdown(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            queue = self._queue
            with self._config.state_lock:
                ready = (
                    self._config.state.shutdown_requested
                    and self._config.state.worker is None
                )
            if not ready or self._submission_active or (queue and not queue.empty()):
                continue
            try:
                await self._persist_exit()
            except Exception as exc:
                self._config.scrollback.emit("error", str(exc))
            app = self._config.prompt_session.app
            await self._config.scrollback.flush(app)
            self._config.scrollback.close()
            app.exit(result="")
            return

    async def _persist_exit(self) -> None:
        future = self._submission_executor.submit(self._config.persist_exit)
        self._persistence_future = future
        try:
            await asyncio.wrap_future(future)
        finally:
            if future.done() and not future.cancelled() and future.exception() is None:
                self._exit_persisted = True
            if future.done():
                self._persistence_future = None

    def _persist_after_abrupt_exit(self) -> None:
        if not self._exit_persisted:
            self._config.persist_exit()
            self._exit_persisted = True
        app = self._config.prompt_session.app
        self._config.scrollback.drain_sync(width=max(1, app.output.get_size().columns))


def _normalize_submitted_prompt(prompt: str) -> str:
    normalized = prompt.strip().lower()
    if normalized in {
        "exit",
        "quit",
        "/compact",
        "/shortcuts",
        "?",
        "/new",
        "/pin",
        "/info",
        "/fork",
        "/tree",
    }:
        return prompt.strip()
    return prompt


def _requires_terminal_control(prompt: str) -> bool:
    normalized = prompt.strip().lower()
    if normalized == "?":
        return True
    command = normalized.partition(" ")[0]
    return command.startswith("/") and command not in {"/compact", "/ps"}


def run_persistent_prompt_application(config: PromptLifecycleConfig) -> int:
    """Run the configured persistent prompt lifecycle."""
    return PersistentPromptLifecycle(config).run()
