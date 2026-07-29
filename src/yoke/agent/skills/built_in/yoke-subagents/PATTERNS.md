# Orchestration Pattern Templates

These templates are branch-specific reference. Copy only the shapes the current
task needs, then keep prompts scoped and artifacts under `.agents_local/`.

Treat these as ideas, not fixed scripts. Mix, change, simplify, or invent a new
pattern when that better serves the user's objective. The goal is useful
orchestration, not adherence to a template.

All orchestration entry points are async. Use `Agent.prompt_async()` for one
stateful role and `run_many()` for independent fan-out. Launch scripts with
`asyncio.run(main())`; do not rebuild thread-pool orchestration around the
synchronous API.

## Quick Audit

Use quick audit for lightweight independent perspectives when full planning is
too much machinery. Keep agents read-only and ask each one for evidence,
confidence, and recommended next action.

```python
AUDIT_ANGLES = ["correctness", "tests", "risk"]


async def quick_audit(user_request: str) -> list[dict[str, object]]:
    tasks = [
        BatchTask(
            id=angle,
            prompt=(
                "Review this request from one angle only. Return concise "
                "findings with evidence and next action.\n\n"
                f"Angle: {angle}\nRequest:\n{user_request}"
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

## Research

Use research when the user asks an open question that needs evidence before a
plan or patch. Choose codebase research, online research, or mixed research.

```python
RESEARCH_MODES = ["codebase", "web", "mixed"]


def research_agent(selection: str | None = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            sys_prompt="Stay read-only and support claims with sources.",
            tools=[
                "file.read",
                "file.search",
                "web.fetch",
                "web.research",
            ],
        ),
    )


async def research(user_question: str) -> list[dict[str, object]]:
    tasks = [
        BatchTask(
            id=mode,
            prompt=(
                "Research this question from the assigned perspective. Return "
                "concise evidence, sources or file paths, confidence, and next "
                f"action.\n\nMode: {mode}\nQuestion:\n{user_question}"
            ),
        )
        for mode in RESEARCH_MODES
    ]
    batch = await run_many(
        tasks,
        agent_factory=lambda task: research_agent(),
        max_concurrency=len(tasks),
    )
    return [batch_item_payload(item, key="mode") for item in batch.items]
```

## Discovery

Use discovery when the main agent does not know the task boundaries. This is a
single stateful role, so call `prompt_async()` directly and close it with an
async context manager.

```python
class DiscoveryItem(BaseModel):
    id: str = Field(description="Stable identifier for the item.")
    path: str = Field(description="Primary file or directory path.")
    summary: str
    suggested_task: str


class DiscoveryPlan(BaseModel):
    items: list[DiscoveryItem]
    risks: list[str] = Field(default_factory=list)


async def discover(user_request: str) -> DiscoveryPlan:
    prompt = (
        "Explore the repository for this broad request and identify concrete "
        "work items. Return only structured data.\n\n"
        f"User request:\n{user_request}"
    )
    async with read_only_agent() as worker:
        result = await worker.prompt_async(prompt, output_type=DiscoveryPlan)
    if result.structured is None:
        raise RuntimeError("Discovery did not return structured output")
    return result.structured
```

## Planning

Use planning to convert discoveries into focused, non-overlapping tasks.

```python
class TaskSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scope: str
    prompt: str
    selection: str | None = DEFAULT_SELECTION


class TaskPlan(BaseModel):
    tasks: list[TaskSpec]
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


async def plan_tasks(
    user_request: str, discoveries: object
) -> TaskPlan:
    prompt = (
        "Create a bounded, non-overlapping task plan for subagents. "
        "Prefer one task per file, route group, package, or concern. "
        "Do not exceed 64 tasks.\n\n"
        f"User request:\n{user_request}\n\n"
        f"Discovery outputs:\n{json_payload(discoveries)}"
    )
    async with read_only_agent() as worker:
        result = await worker.prompt_async(prompt, output_type=TaskPlan)
    if result.structured is None:
        raise RuntimeError("Planning did not return structured output")
    task_ids = [task.id for task in result.structured.tasks]
    if len(task_ids) > 64:
        raise ValueError("Task plan exceeds the 64-task cap")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Task IDs must be unique")
    return result.structured
```

## Fan-Out

Use fan-out when tasks can be performed independently. Ask every worker for
evidence, changed files, validation, and risks. Inspect every item status before
trusting the batch.

```python
class TaskResult(BaseModel):
    id: str
    scope: str
    summary: str
    evidence: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


async def fan_out(
    tasks: list[TaskSpec], user_request: str
) -> list[dict[str, object]]:
    if len(tasks) > 64:
        raise ValueError("Fan-out exceeds the 64-task cap")
    task_by_id = {task.id: task for task in tasks}
    if len(task_by_id) != len(tasks):
        raise ValueError("Task IDs must be unique")
    batch_tasks = [
        BatchTask(
            id=task.id,
            prompt=(
                "Complete only this assigned task. Return structured findings "
                "with evidence, changed files, validation, and risks.\n\n"
                f"Overall request:\n{user_request}\n\n"
                f"Task id: {task.id}\nScope: {task.scope}\n\n{task.prompt}"
            ),
        )
        for task in tasks
    ]

    def worker_factory(batch_task: BatchTask) -> Agent:
        return agent(task_by_id[batch_task.id].selection)

    batch = await run_many(
        batch_tasks,
        agent_factory=worker_factory,
        max_concurrency=min(MAX_CONCURRENCY, len(batch_tasks) or 1),
        output_type=TaskResult,
        max_attempts=2,
        on_progress=log_progress,
    )
    results: list[dict[str, object]] = []
    for item in batch.items:
        spec = task_by_id[item.task.id]
        if item.result is None:
            results.append(
                {
                    "id": spec.id,
                    "selection": spec.selection,
                    "status": item.status,
                    "error": repr(item.error),
                }
            )
            continue
        results.append(
            {
                "id": spec.id,
                "selection": spec.selection,
                "status": item.status,
                "structured": (
                    item.result.structured.model_dump()
                    if item.result.structured
                    else None
                ),
                "output": item.result.output,
            }
        )
    return results
```

## Coder/Reviewer Pair

Use one coder and one reviewer per task. Re-prompt the same instances
sequentially so they retain task context. Make reviewer agents durable when a
review loop may span main-agent fixes or process interruptions.

```python
class PairReview(BaseModel):
    verdict: Literal["ok", "nok"]
    feedback: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


async def run_coder_reviewer_pair(
    task: TaskSpec,
    user_request: str,
) -> dict[str, object]:
    coder = Agent(
        provider=build_builtin_provider(task.selection),
        config=RunConfig(
            root=Path.cwd(),
            tools=["file.read", "file.search", "file.write"],
        ),
    )
    reviewer = Agent(
        provider=build_builtin_provider(task.selection),
        config=RunConfig(root=Path.cwd(), tools=["file.read", "file.search"]),
        state_path=OUTPUT_DIR / f"{task.id}.reviewer.json",
        autosave=True,
    )

    async with coder, reviewer:
        coder_result = await coder.prompt_async(
            "Complete only this task. Report changed files and validation.\n\n"
            f"Overall request:\n{user_request}\n\nTask:\n{task.prompt}",
        )
        coder_output = coder_result.output
        for _iteration in range(1, 4):
            review = await reviewer.prompt_async(
                "Review for correctness, scope, tests, and risks. Return ok "
                f"only when ready to merge.\n\nCoder output:\n{coder_output}",
                output_type=PairReview,
            )
            if review.structured and review.structured.verdict == "ok":
                return {
                    "task": task.id,
                    "status": "accepted",
                    "output": coder_output,
                }
            feedback = (
                review.structured.feedback
                if review.structured
                else [review.output]
            )
            revision = await coder.prompt_async(
                "Revise using this feedback and keep scope.\n\n"
                f"Feedback:\n{json.dumps(feedback, indent=2)}",
            )
            coder_output = revision.output
        return {
            "task": task.id,
            "status": "needs_main_agent",
            "output": coder_output,
        }
```

## Review and Coverage

Use review when correctness matters or discovery may be incomplete.

```python
class ReviewResult(BaseModel):
    passed: bool
    missing_coverage: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


async def review_results(
    user_request: str,
    discoveries: object,
    results: object,
) -> ReviewResult:
    prompt = (
        "Review these subagent results for coverage, correctness, conflicts, "
        "and unsupported claims. Compare against discovery outputs.\n\n"
        f"User request:\n{user_request}\n\n"
        f"Discoveries:\n{json_payload(discoveries)}\n\n"
        f"Results:\n{json_payload(results)}"
    )
    async with read_only_agent() as worker:
        result = await worker.prompt_async(prompt, output_type=ReviewResult)
    if result.structured is None:
        raise RuntimeError("Review did not return structured output")
    return result.structured
```

## Merge Handoff

Use merge to produce the final handoff for the main agent.

```python
async def merge_handoff(user_request: str, payload: object) -> str:
    prompt = (
        "Synthesize a final handoff for the main agent. Include summary, "
        "validated findings, conflicts, risks, changed files, and next actions.\n\n"
        f"User request:\n{user_request}\n\n"
        f"Payload:\n{json_payload(payload)}"
    )
    async with read_only_agent() as worker:
        result = await worker.prompt_async(prompt)
    return result.output
```

## Shared Batch Helpers

Keep item and progress handling explicit so failures cannot disappear inside a
successful overall batch.

```python
def batch_item_payload(item, *, key: str) -> dict[str, object]:
    payload: dict[str, object] = {
        key: item.task.id,
        "status": item.status,
        "attempts": item.attempts,
    }
    if item.result is not None:
        payload["output"] = item.result.output
    else:
        payload["error"] = repr(item.error)
    return payload


def log_progress(progress: BatchProgress) -> None:
    LOGGER.info(
        "Task finish id=%s status=%s progress=%d/%d attempts=%d",
        progress.task_id,
        progress.status,
        progress.completed,
        progress.total,
        progress.attempts,
    )
```
