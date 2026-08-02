# Discovery, planning, and fan-out

Read [`../COMMON.md`](../COMMON.md) first. Use this branch when task boundaries
must be discovered before independent work begins.

## Contents

- [Models](#models)
- [Discover and plan](#discover-and-plan)
- [Fan out](#fan-out)

## Models

```python
from pydantic import BaseModel, Field

from yoke.ai import BatchTask, run_many


class DiscoveryItem(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    path: str
    summary: str
    suggested_task: str


class DiscoveryPlan(BaseModel):
    items: list[DiscoveryItem]
    risks: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    tasks: list[TaskSpec]
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    id: str
    summary: str
    evidence: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
```

## Discover and plan

```python
async def discover(user_request: str) -> DiscoveryPlan:
    async with read_only_agent() as worker:
        result = await worker.prompt_async(
            "Identify concrete repository work items. Return only structured "
            f"data.\n\nRequest:\n{user_request}",
            output_type=DiscoveryPlan,
        )
    result.require_completed()
    if result.structured is None:
        raise RuntimeError("Discovery returned no structured value")
    return result.structured


async def plan_tasks(
    user_request: str,
    discoveries: DiscoveryPlan,
) -> TaskPlan:
    async with read_only_agent() as worker:
        result = await worker.prompt_async(
            "Create non-overlapping tasks. Use filename-safe unique IDs, mark "
            "workspace mutation accurately, and return at most 64 tasks.\n\n"
            f"Request:\n{user_request}\n\n"
            f"Discoveries:\n{json_payload(discoveries)}",
            output_type=TaskPlan,
        )
    result.require_completed()
    if result.structured is None:
        raise RuntimeError("Planning returned no structured value")
    plan = result.structured
    ids = [task.id for task in plan.tasks]
    if len(ids) > 64:
        raise ValueError("Task plan exceeds the 64-task cap")
    if len(ids) != len(set(ids)):
        raise ValueError("Task IDs must be unique")
    for task in plan.tasks:
        validate_slug(task.id, label="task id")
    return plan
```

## Fan out

Preflight all selections before `run_many()`. Mutating tasks default to one
attempt. If a caller enables more, require an explicit statement that retries
re-read the current tree and are safe after partial writes.

```python
async def fan_out(
    tasks: list[TaskSpec],
    user_request: str,
    *,
    mutation_attempts: int = 1,
) -> list[dict[str, object]]:
    if len(tasks) > 64:
        raise ValueError("Fan-out exceeds the 64-task cap")
    task_by_id = {task.id: task for task in tasks}
    if len(task_by_id) != len(tasks):
        raise ValueError("Task IDs must be unique")
    if mutation_attempts < 1:
        raise ValueError("mutation_attempts must be positive")
    preflight_selections({task.selection for task in tasks})

    batch_tasks = [
        BatchTask(
            id=task.id,
            prompt=(
                "Complete only this task. Re-read the current workspace before "
                "editing. Return evidence, exact changed files, validation, and "
                "risks.\n\n"
                f"Overall request:\n{user_request}\n\n"
                f"Task id: {task.id}\nScope: {task.scope}\n\n{task.prompt}"
            ),
        )
        for task in tasks
    ]

    def factory(batch_task: BatchTask) -> Agent:
        spec = task_by_id[batch_task.id]
        LOGGER.info("task_start id=%s selection=%s", spec.id, spec.selection)
        return (
            coding_agent(spec.selection)
            if spec.mutates_workspace
            else read_only_agent(spec.selection)
        )

    # Split mutation policies if read-only tasks need retries. One shared value is
    # intentionally conservative for a mixed batch.
    max_attempts = mutation_attempts if any(t.mutates_workspace for t in tasks) else 2
    batch = await run_many(
        batch_tasks,
        agent_factory=factory,
        max_concurrency=min(MAX_CONCURRENCY, len(batch_tasks) or 1),
        output_type=TaskResult,
        max_attempts=max_attempts,
        on_progress=log_progress,
    )
    require_batch_integrity(batch)

    results: list[dict[str, object]] = []
    for item in batch.items:
        spec = task_by_id[item.task.id]
        if item.status != "completed" or item.result is None:
            results.append(
                {
                    "id": spec.id,
                    "selection": spec.selection,
                    "status": item.status,
                    "error": item.error,
                }
            )
            continue
        item.result.require_completed()
        structured = item.result.structured
        if structured is None:
            raise RuntimeError(f"Task {spec.id} returned no structured value")
        if structured.id != spec.id:
            raise RuntimeError(f"Task {spec.id} returned mismatched id {structured.id}")
        results.append(
            {
                "id": spec.id,
                "selection": spec.selection,
                "status": item.status,
                "result": structured,
            }
        )
    return results
```

Before merging, verify that reported changed files stay within assigned scopes and
that concurrent workers did not overlap.
