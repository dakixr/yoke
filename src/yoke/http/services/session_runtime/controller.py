"""Turn controller and physical completion ownership."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Lock
from typing import TYPE_CHECKING

from yoke.http.services.session_runtime.completion import retain_cancelled_worker
from yoke.http.services.session_runtime.execution import TurnExecution, TurnOutcome
from yoke.http.services.session_runtime.owned_cleanup import execute_with_cleanup

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources


def retire_owned(
    resources: SessionRuntimeResources,
    agent: object | None,
    preparation_cleanup: list[Future[None]],
) -> Future[None]:
    """Include cleanup started by a failed agent preparation before it returned."""
    owned = resources.retire(agent)
    pending = list(preparation_cleanup)
    if owned is not None and owned not in pending:
        pending.append(owned)
    completion: Future[None] = Future()
    remaining = len(pending)
    lock = Lock()

    def finished(_done: Future[None]) -> None:
        nonlocal remaining
        with lock:
            remaining -= 1
            if remaining == 0:
                completion.set_result(None)

    if not pending:
        completion.set_result(None)
    for future in pending:
        future.add_done_callback(finished)
    return completion


def launch_controller(runtime: SessionRuntime, execution: TurnExecution) -> None:
    tracker = runtime.workers
    tracker.admit(execution.turn_id)

    def finished(_task: asyncio.Task[None]) -> None:
        try:
            if not execution.worker_started:
                execution.workspace_use.close()
        finally:
            tracker.release(execution.turn_id)

    execution.task = asyncio.create_task(
        run_execution(runtime, execution),
        name=f"yoke-http-session-{runtime.session_id}-{execution.turn_id}",
    )
    execution.task.add_done_callback(finished)


async def run_execution(runtime: SessionRuntime, execution: TurnExecution) -> None:
    worker: Future[TurnOutcome] | None = None
    completion: Future[None] = Future()
    cancelled = False
    preparation_cleanup: list[Future[None]] = []
    loop = asyncio.get_running_loop()
    try:
        if execution.cold_start:
            # Preserve the admission response's head start on cold session loads.
            await asyncio.sleep(0.05)
        await runtime.active_slots.acquire()
        execution.slot_acquired = True
        if execution.retired_event.is_set():
            return
        worker = runtime.executor.submit(
            lambda: execute_with_cleanup(
                lambda: runtime._execute_sync(execution, loop), preparation_cleanup
            ),
        )
        execution.worker_started = True
        runtime.workers.retain(execution.turn_id, completion)
        outcome = await asyncio.shield(asyncio.wrap_future(worker))
        await runtime._finish_execution(execution, outcome)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as exc:  # controller finalization boundary
        await runtime._finish_execution_error(execution, exc)
    finally:
        if worker is None:
            runtime._release_slot(execution)
        else:
            retain_cancelled_worker(
                worker,
                loop=loop,
                retire_agent=lambda agent: retire_owned(
                    runtime.resources, agent, preparation_cleanup
                ),
                release_slot=lambda: runtime._release_slot(execution),
                release_resources=execution.workspace_use.close,
                completed=lambda: completion.set_result(None),
            )
            if not cancelled:
                await asyncio.shield(asyncio.wrap_future(completion))
