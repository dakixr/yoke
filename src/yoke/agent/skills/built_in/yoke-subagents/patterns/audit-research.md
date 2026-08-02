# Quick audit and research

Read [`../COMMON.md`](../COMMON.md) first for shared helpers.

## Contents

- [Quick audit](#quick-audit)
- [Research](#research)

## Quick audit

Use 2-4 read-only angles when independent perspectives add value without a
planning phase.

```python
from yoke.ai import BatchTask, run_many

AUDIT_ANGLES = ("correctness", "tests", "risk")


async def quick_audit(user_request: str) -> list[dict[str, object]]:
    tasks = [
        BatchTask(
            id=angle,
            prompt=(
                "Review only the assigned angle. Return evidence, confidence, "
                "uncertainties, and a recommended next action.\n\n"
                f"Angle: {angle}\nRequest:\n{user_request}"
            ),
        )
        for angle in AUDIT_ANGLES
    ]

    def factory(task: BatchTask) -> Agent:
        LOGGER.info("task_start id=%s selection=%s", task.id, DEFAULT_SELECTION)
        return read_only_agent()

    batch = await run_many(
        tasks,
        agent_factory=factory,
        max_concurrency=len(tasks),
        on_progress=log_progress,
    )
    require_batch_integrity(batch)
    results: list[dict[str, object]] = []
    for item in batch.items:
        if item.status != "completed" or item.result is None:
            results.append(
                {"id": item.task.id, "status": item.status, "error": item.error}
            )
            continue
        item.result.require_completed()
        results.append(
            {"id": item.task.id, "status": item.status, "output": item.result.output}
        )
    return results
```

## Research

Add web or image capabilities only when the assigned mode needs external or
visual evidence.

```python
def research_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            sys_prompt="Stay read-only and support claims with cited sources.",
            tools=[
                "file.read",
                "file.search",
                "file.extract_context",
                "image.attach",
                "web.fetch",
                "web.search",
                "web.research",
            ],
        ),
    )


async def research(user_question: str) -> list[dict[str, object]]:
    modes = ("codebase", "web", "mixed")
    tasks = [
        BatchTask(
            id=mode,
            prompt=(
                "Research only the assigned mode. Return evidence, source paths "
                "or URLs, confidence, and remaining uncertainty.\n\n"
                f"Mode: {mode}\nQuestion:\n{user_question}"
            ),
        )
        for mode in modes
    ]

    def factory(task: BatchTask) -> Agent:
        LOGGER.info("task_start id=%s selection=%s", task.id, DEFAULT_SELECTION)
        return research_agent()

    batch = await run_many(
        tasks,
        agent_factory=factory,
        max_concurrency=len(tasks),
        on_progress=log_progress,
    )
    require_batch_integrity(batch)
    return [
        {
            "mode": item.task.id,
            "status": item.status,
            "output": (
                item.result.require_completed().output if item.result else None
            ),
            "error": item.error,
        }
        for item in batch.items
    ]
```

Write the returned list with `write_artifact()`. A later review role must compare
claims across modes and reject unsupported synthesis.
