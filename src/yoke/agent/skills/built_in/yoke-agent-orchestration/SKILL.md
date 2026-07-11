---
name: yoke-agent-orchestration
description: Use when creating Yoke SDK agents programmatically or coordinating multiple agents for planning, delegation, review, fan-out, merging, iterative refinement, or dependency-driven pipeline execution.
---

# Yoke Agent Orchestration

Use the `yoke.ai` SDK and normal Python control flow. Keep orchestration small,
typed, and explicit.

## Set up a provider

Check the selected provider, then construct it from a qualified reference:

```python
from yoke.ai import build_provider, provider_status

provider_ref = "zai:glm-5.2:none"
status = provider_status(provider_ref)
if not status.ready:
    raise RuntimeError(status.reason or f"Provider is not ready: {provider_ref}")
provider = build_provider(provider_ref)
```

Use the provider requested by the user or select one that is ready. Create a
fresh provider per agent unless its implementation explicitly supports sharing.

## Create an agent

```python
from pathlib import Path

from pydantic import BaseModel

from yoke.ai import Agent, RunConfig, build_provider


class Review(BaseModel):
    findings: list[str]
    approved: bool


root = Path.cwd()
agent = Agent(
    provider=build_provider(provider_ref),
    config=RunConfig(root=root),
)
result = agent.prompt("Review src/yoke/foo.py", output_type=Review)
assert result.structured is not None
print(result.structured.model_dump_json(indent=2))
```

Use Pydantic `output_type` whenever code or another agent consumes the result.
Reuse the same `Agent` for a continuing conversation. Create another `Agent`
when the role needs isolated instructions and conversation history.

## Coordinate agents

A useful orchestration usually has three explicit phases:

1. A planner returns typed work items.
2. Isolated workers handle the items.
3. A merger consumes the worker results and returns the final typed result.

```python
plan = planner.prompt(task, output_type=Plan).structured
assert plan is not None

results: list[WorkerResult] = []
for item in plan.items:
    result = worker.prompt(
        item.model_dump_json(),
        output_type=WorkerResult,
    ).structured
    assert result is not None
    results.append(result)

final = merger.prompt(
    "Merge these results:\n"
    + "\n".join(item.model_dump_json() for item in results),
    output_type=FinalResult,
).structured
assert final is not None
```

For independent parallel work, create one agent per worker and use the host
application's executor. Respect provider rate limits. Do not concurrently call
the same stateful agent.

## Pipeline dependent work

For a fixed small batch, finish all workers and then merge. For a larger or
dynamic dependency graph, prefer ready-node pipeline scheduling: start a task as
soon as its own dependencies complete instead of waiting for every task at the
same graph depth.

```text
A investigate -> A implement -> A review
B investigate ----------------> B implement -> B review
C investigate -> discover C1/C2 -> process both
```

This is an asynchronous DAG traversal with a soft depth-first preference, not a
global breadth-first barrier. Process the first completed task, update the graph,
and immediately launch newly ready dependents:

```python
async def run_task(task: Task, agent: Agent) -> TaskResult:
    response = await asyncio.to_thread(
        agent.prompt,
        task.model_dump_json(),
        output_type=TaskResult,
    )
    assert response.structured is not None
    return response.structured


while pending or running:
    for task in ready_tasks(pending, completed):
        agent = agent_for(task)
        future = asyncio.create_task(run_task(task, agent))
        running[future] = task

    done, _ = await asyncio.wait(
        running,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for future in done:
        task = running.pop(future)
        result = future.result()
        completed[task.id] = result
        pending.update(result.discovered_tasks)
```

Do not use `gather()` around a ready batch when downstream work should start
before the slowest sibling completes; that creates a level barrier. Bound the
number of running tasks and apply aging so a deep or expanding branch cannot
starve older ready tasks.

`Agent.prompt()` is synchronous; the SDK has no `prompt_async()` method. Use
`asyncio.to_thread`, a bounded thread executor, or another host-level executor
to run independent agents concurrently.

For production-oriented code, adapt the maintained scheduler example at
`src/yoke/docs/examples/agent_pipeline.py` instead of reconstructing the task
maps and completion loop from abbreviated pseudocode.

Keep one task-owned agent for successive steps that benefit from the branch's
conversation context. Use separate agents for independent branches and isolated
reviewers. A task may add typed child tasks, but every child must declare its
dependencies. Propagate failure only to dependent tasks when possible; allow
unrelated branches to continue. Bound review loops and dynamic expansion.

## Use condition-controlled loops

```python
class Decision(BaseModel):
    approved: bool
    feedback: str


request = initial_request
for _ in range(5):
    candidate = coder.prompt(request).text
    decision = reviewer.prompt(
        candidate,
        output_type=Decision,
    ).structured
    assert decision is not None
    if decision.approved:
        break
    request = f"Revise using this feedback: {decision.feedback}"
else:
    raise RuntimeError("Agent loop did not converge")
```

Always include a loop limit. Exit based on structured state, not a hard-coded
sequence that ignores the agent's decision.

## Hygiene

- Give each agent one clear role.
- Validate every structured boundary.
- Keep prompts focused and pass only the context the role needs.
- Handle provider, validation, and convergence failures explicitly.
- Start ready dependent work promptly; avoid unnecessary phase barriers.
- Limit concurrency, dynamic expansion, and retries.
- Close agents after use in long-lived processes.
- Put disposable scripts under an ignored path such as `.yoke/manual/`.
- Commit orchestration scripts only when they are maintained product code.

Completion criterion: the script uses ready providers, has explicit typed
handoffs, preserves or isolates conversation state intentionally, and has clear
failure and termination behavior.
