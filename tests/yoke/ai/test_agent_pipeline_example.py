from __future__ import annotations

import asyncio
import runpy
from pathlib import Path
from typing import Any
from typing import cast


_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "yoke"
    / "docs"
    / "examples"
    / "agent_pipeline.py"
)


def _example_symbols() -> dict[str, Any]:
    return runpy.run_path(str(_EXAMPLE), run_name="agent_pipeline_example")


def test_pipeline_starts_discovered_work_before_slow_sibling_finishes() -> None:
    symbols = _example_symbols()
    task_type = symbols["Task"]
    result_type = symbols["TaskResult"]
    run_pipeline = symbols["run_pipeline"]
    events: list[str] = []

    async def run_task(task) -> object:
        events.append(f"start:{task.id}")
        if task.id == "slow":
            await asyncio.sleep(0.03)
        else:
            await asyncio.sleep(0)
        events.append(f"end:{task.id}")
        children = [task_type(id="child", prompt="child")] if task.id == "fast" else []
        return result_type(summary=task.id, discovered_tasks=children)

    completed, failed = asyncio.run(
        run_pipeline(
            [
                task_type(id="slow", prompt="slow"),
                task_type(id="fast", prompt="fast"),
            ],
            run_task,
            max_concurrency=2,
        )
    )

    assert set(completed) == {"slow", "fast", "child"}
    assert failed == {}
    assert events.index("start:child") < events.index("end:slow")


def test_pipeline_blocks_failed_descendants_but_finishes_other_branches() -> None:
    symbols = _example_symbols()
    task_type = symbols["Task"]
    result_type = symbols["TaskResult"]
    run_pipeline = symbols["run_pipeline"]

    async def run_task(task) -> object:
        if task.id == "failure":
            raise RuntimeError("expected")
        return result_type(summary=task.id)

    completed, failed = asyncio.run(
        run_pipeline(
            [
                task_type(id="failure", prompt="fail"),
                task_type(
                    id="dependent",
                    prompt="blocked",
                    dependencies={"failure"},
                ),
                task_type(id="independent", prompt="continue"),
            ],
            cast(Any, run_task),
        )
    )

    assert set(completed) == {"independent"}
    assert set(failed) == {"failure", "dependent"}
    assert failed["dependent"] == "blocked by a failed dependency"
