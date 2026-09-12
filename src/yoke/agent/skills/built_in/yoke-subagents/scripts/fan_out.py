"""Run independent read-only audits or research tasks from a JSON task list."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

# Multiprocessing also imports this file as __mp_main__ when spawning tool workers.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "scripts"

from pydantic import BaseModel, Field, TypeAdapter

from yoke.ai import Agent, AgentObserver, BatchTask, RunConfig, build_builtin_provider
from yoke.ai import run_many

from .orchestration_support import TaskSpec, finish, log_phase, observer_for, parser
from .orchestration_support import preflight, prepare_run, read_tools


class Findings(BaseModel):
    """Evidence stays distinct from unexecuted validation and blockers."""

    summary: str
    evidence: list[str]
    validation: list[str]
    risks: list[str] = Field(default_factory=list)
    blocker: str | None = None


async def audit(
    tasks: list[TaskSpec],
    *,
    root: Path,
    selection: str,
    observer: AgentObserver,
    max_concurrency: int = 4,
) -> dict[str, object]:
    """Inspect every terminal outcome; one failed worker cannot disappear."""
    if not 1 <= len(tasks) <= 64 or not 1 <= max_concurrency <= 16:
        raise ValueError("Use 1-64 tasks and 1-16 concurrent workers")
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("Task IDs must be unique")

    def factory(task: BatchTask) -> Agent:
        return Agent(
            provider=build_builtin_provider(selection),
            config=RunConfig(
                root=root,
                tools=read_tools(by_id[task.id].mode),
                sys_prompt="Stay read-only. Cite evidence and distinguish facts from guesses.",
            ),
        )

    batch = await run_many(
        [
            BatchTask(
                id=task.id,
                prompt="Audit only this contract. Report unexecuted checks honestly.\n"
                + task.model_dump_json(indent=2),
            )
            for task in tasks
        ],
        agent_factory=factory,
        max_concurrency=max_concurrency,
        max_attempts=1,
        output_type=Findings,
        observer=observer,
    )
    items: list[dict[str, object]] = []
    needs_attention = bool(batch.progress_errors)
    for item in batch.items:
        findings = item.result.structured if item.result is not None else None
        needs_attention |= (
            item.status != "completed" or findings is None or bool(findings.blocker)
        )
        items.append(
            {
                "id": item.task.id,
                "status": item.status,
                "attempts": item.attempts,
                "findings": findings,
                "output": item.result.output if item.result is not None else None,
                "error": repr(item.error) if item.error is not None else None,
            }
        )
    return {
        "status": "needs_main_agent" if needs_attention else "completed",
        "selection": selection,
        "tasks": tasks,
        "items": items,
        "usage": batch.usage,
        "progress_errors": [repr(error) for error in batch.progress_errors],
    }


async def main() -> int:
    """Read explicit inputs and persist both successful and failed runs."""
    cli = parser(__doc__ or "Read-only fan-out", "--tasks")
    cli.add_argument("--max-concurrency", type=int, default=4)
    args = cli.parse_args()
    root = args.root.resolve()
    run_dir = prepare_run(root, args.run_id)
    try:
        tasks = TypeAdapter(list[TaskSpec]).validate_json(args.tasks.read_text())
        await preflight(args.selection, root)
        log_phase(run_dir, f"Audit: {len(tasks)} tasks; selection: {args.selection}")
        payload = await audit(
            tasks,
            root=root,
            selection=args.selection,
            observer=observer_for(run_dir),
            max_concurrency=args.max_concurrency,
        )
    except Exception as error:
        payload = {"status": "error", "error": repr(error)}
    return finish(run_dir, payload)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
