# Orchestration Pattern Templates

Copy only the shape the task needs. Keep prompts scoped and artifacts under
`.agents_local/`. These patterns are adaptable, not fixed scripts.

All entry points are async. Use `Agent.prompt_async()` for stateful roles and
`run_many()` for independent fan-out.

## Quick Audit

Use 2-4 read-only agents for lightweight independent perspectives.

```python
AUDIT_ANGLES = ["correctness", "tests", "risk"]


async def quick_audit(request: str) -> list[dict[str, object]]:
    tasks = [
        BatchTask(
            id=angle,
            prompt=(
                "Review from this angle only. Return evidence and a next "
                f"action.\n\nAngle: {angle}\nRequest:\n{request}"
            ),
        )
        for angle in AUDIT_ANGLES
    ]
    batch = await run_many(
        tasks,
        agent_factory=lambda task: read_only_agent(),
        max_concurrency=len(tasks),
    )
    return [batch_item_payload(item, key="angle") for item in batch.items]
```

## Discovery and Planning

Use typed, read-only roles when boundaries are unknown, then convert discoveries
into non-overlapping task specs.

```python
class DiscoveryItem(BaseModel):
    id: str
    path: str
    summary: str
    suggested_task: str


class DiscoveryPlan(BaseModel):
    items: list[DiscoveryItem]
    risks: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scope: str
    prompt: str
    selection: str = DEFAULT_SELECTION


class TaskPlan(BaseModel):
    tasks: list[TaskSpec]
    risks: list[str] = Field(default_factory=list)


async def typed_role(prompt: str, output_type):
    async with read_only_agent() as agent:
        result = await agent.prompt_async(prompt, output_type=output_type)
    if result.structured is None:
        raise RuntimeError("Role returned no structured output")
    return result.structured
```

Validate unique task IDs, non-overlapping scopes, and the 64-task cap before
fan-out.

## Fan-Out

Use fresh agents/providers, exclusive scopes, and typed terminal results.

```python
class TaskResult(BaseModel):
    id: str
    scope: str
    summary: str
    evidence: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


async def fan_out(tasks: list[TaskSpec], request: str) -> list[dict[str, object]]:
    if len(tasks) > 64 or len({task.id for task in tasks}) != len(tasks):
        raise ValueError("Tasks must have unique IDs and stay within the cap")
    task_by_id = {task.id: task for task in tasks}
    batch_tasks = [
        BatchTask(
            id=task.id,
            prompt=(
                "Complete only this scope. Return evidence, changed files, "
                "validation, and risks.\n\n"
                f"Overall request:\n{request}\n\nTask:\n{task.model_dump_json()}"
            ),
        )
        for task in tasks
    ]

    def factory(batch_task: BatchTask) -> Agent:
        spec = task_by_id[batch_task.id]
        return Agent(
            provider=build_builtin_provider(spec.selection),
            config=RunConfig(
                root=Path.cwd(),
                tools=["file.read", "file.search", "file.write"],
            ),
        )

    batch = await run_many(
        batch_tasks,
        agent_factory=factory,
        max_concurrency=min(MAX_CONCURRENCY, len(batch_tasks) or 1),
        output_type=TaskResult,
        max_attempts=2,
        on_progress=log_progress,
    )
    return [batch_item_payload(item, key="task") for item in batch.items]
```

Inspect every exception. Confirm each changed file lies within its task's
exclusive scope before merging.

## Coder/Reviewer Pair

Reuse each role sequentially so it retains conversation. Make the reviewer
durable and bound the loop.

```python
class PairReview(BaseModel):
    verdict: Literal["ok", "nok"]
    feedback: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


async def coder_reviewer(task: TaskSpec, request: str) -> dict[str, object]:
    coder = Agent(
        provider=build_builtin_provider(task.selection),
        config=RunConfig(
            root=Path.cwd(),
            tools=["file.read", "file.search", "file.write"],
        ),
    )
    reviewer = Agent(
        provider=build_builtin_provider(task.selection),
        config=RunConfig(
            root=Path.cwd(),
            tools=["file.read", "file.search"],
        ),
        state_path=OUTPUT_DIR / f"{task.id}.reviewer.json",
        autosave=True,
    )
    async with coder, reviewer:
        output = (
            await coder.prompt_async(
                f"Complete only this task.\n\nRequest:\n{request}\n\n{task.prompt}"
            )
        ).output
        for _ in range(3):
            review = await reviewer.prompt_async(
                "Review correctness, scope, tests, and risk. Return ok only "
                f"when ready.\n\nCoder output:\n{output}",
                output_type=PairReview,
            )
            if review.structured and review.structured.verdict == "ok":
                return {"task": task.id, "status": "accepted", "output": output}
            feedback = review.structured.feedback if review.structured else [review.output]
            output = (
                await coder.prompt_async(
                    f"Revise within scope using:\n{json_payload(feedback)}"
                )
            ).output
    return {"task": task.id, "status": "needs-main-agent", "output": output}
```

## Review and Merge Handoff

Use a final read-only role to compare results with the request and discoveries.
Require missing coverage, conflicts, unsupported claims, task errors, changed
files, risks, and next actions. The main agent reads both this handoff and the
raw results before acting.

## Dependency-Driven Pipeline

When downstream work should start before unrelated siblings finish, use the
ready-node scheduler in `src/yoke/docs/examples/agent_pipeline.py`. Do not add a
breadth-first `gather()` barrier.

## Shared Result Helper

```python
def batch_item_payload(item, *, key: str) -> dict[str, object]:
    payload: dict[str, object] = {
        key: item.task.id,
        "status": item.status,
        "attempts": item.attempts,
    }
    if item.result is not None:
        payload["output"] = item.result.output
        if item.result.structured is not None:
            payload["structured"] = item.result.structured.model_dump()
    else:
        payload["error"] = repr(item.error)
    return payload
```
