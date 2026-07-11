"""Dependency-driven Yoke agent pipeline example.

Run from a configured workspace with:

    uv run python src/yoke/docs/examples/agent_pipeline.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from typing import cast

from pydantic import BaseModel
from pydantic import Field

from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai import build_provider
from yoke.ai import provider_status


class Task(BaseModel):
    """One node in the dynamically expanding task graph."""

    id: str
    prompt: str
    dependencies: set[str] = Field(default_factory=set)


class TaskResult(BaseModel):
    """Typed worker output, including optional newly discovered work."""

    summary: str
    discovered_tasks: list[Task] = Field(default_factory=list)


TaskRunner = Callable[[Task], Coroutine[Any, Any, TaskResult]]


async def run_pipeline(
    initial_tasks: list[Task],
    run_task: TaskRunner,
    *,
    max_concurrency: int = 4,
    max_tasks: int = 50,
) -> tuple[dict[str, TaskResult], dict[str, str]]:
    """Run ready tasks without waiting for unrelated graph levels."""
    pending = {task.id: task for task in initial_tasks}
    known_ids = set(pending)
    completed: dict[str, TaskResult] = {}
    failed: dict[str, str] = {}
    running: dict[asyncio.Task[TaskResult], Task] = {}

    while pending or running:
        blocked_ids = _blocked_task_ids(pending, set(failed))
        for task_id in blocked_ids:
            failed[task_id] = "blocked by a failed dependency"
            pending.pop(task_id)

        capacity = max_concurrency - len(running)
        ready = [
            task for task in pending.values() if task.dependencies <= completed.keys()
        ]
        for task in ready[:capacity]:
            pending.pop(task.id)
            future = asyncio.create_task(run_task(task))
            running[future] = task

        if not running:
            if pending:
                unresolved = ", ".join(sorted(pending))
                raise RuntimeError(f"Task graph is cyclic or unresolved: {unresolved}")
            break

        done, _ = await asyncio.wait(
            running,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for future in done:
            task = running.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                failed[task.id] = f"{type(exc).__name__}: {exc}"
                continue

            completed[task.id] = result
            for child in result.discovered_tasks:
                if child.id in known_ids:
                    raise RuntimeError(f"Duplicate task id: {child.id}")
                if len(known_ids) >= max_tasks:
                    raise RuntimeError("Dynamic task limit exceeded")
                child.dependencies.add(task.id)
                pending[child.id] = child
                known_ids.add(child.id)

    return completed, failed


def _blocked_task_ids(
    pending: dict[str, Task],
    failed_ids: set[str],
) -> set[str]:
    blocked: set[str] = set()
    changed = True
    while changed:
        changed = False
        unavailable = failed_ids | blocked
        for task in pending.values():
            if task.id not in blocked and task.dependencies & unavailable:
                blocked.add(task.id)
                changed = True
    return blocked


def yoke_task_runner(
    *,
    provider_ref: str,
    root: Path,
) -> TaskRunner:
    """Build a runner that gives every concurrent task an isolated agent."""

    async def run_task(task: Task) -> TaskResult:
        agent = Agent(
            provider=build_provider(provider_ref),
            config=RunConfig(root=root),
        )
        try:
            response = await asyncio.to_thread(
                agent.prompt,
                task.prompt,
                output_type=TaskResult,
            )
        finally:
            agent.close()
        if response.structured is None:
            raise RuntimeError(f"Task {task.id} returned no structured result")
        return cast(TaskResult, response.structured)

    return run_task


async def main() -> None:
    """Run three independent roots that may discover dependent children."""
    provider_ref = "zai:glm-5.2:none"
    status = provider_status(provider_ref)
    if not status.ready:
        raise RuntimeError(status.reason or f"Provider is not ready: {provider_ref}")

    initial_tasks = [
        Task(id="schema", prompt="Analyze the schema migration."),
        Task(id="api", prompt="Plan the API migration."),
        Task(id="tests", prompt="Plan the test migration."),
    ]
    completed, failed = await run_pipeline(
        initial_tasks,
        yoke_task_runner(provider_ref=provider_ref, root=Path.cwd()),
    )
    print(f"completed: {', '.join(sorted(completed))}")
    print(f"failed: {', '.join(sorted(failed)) or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
