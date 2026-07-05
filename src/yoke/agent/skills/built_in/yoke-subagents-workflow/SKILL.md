---
name: yoke-subagents-workflow
description: Use when creating Yoke AI subagents programmatically, deciding whether a task needs a long-running observable workflow, or helping users follow a Yoke workflow through CLI/web Observe.
---

# Yoke Subagents Workflow

Use this skill when a task needs programmatic Yoke AI agents, subagent-style
coordination, or a long-running observable workflow. Do not use it for quick
single-prompt work where a normal answer or one tool call is enough.

## Choose The Shape

Pick the lightest shape that fits the job:

- Quick task: answer directly or use normal tools. Do not create workflow
  scripts or Observe runs.
- One-off subagent script: use `yoke.ai` to create one or more `Agent`
  instances programmatically for a bounded task.
- Long workflow: use `yoke.observe.workflow` and `@step` when the task will take
  time, fan out, loop, merge, or needs user/agent inspection while it runs.

Completion criterion: the chosen shape is justified by task size. Long-running
coordination gets Observe; quick work does not.

## Provider Setup

Start scripts by listing provider readiness, then build the chosen provider
from a qualified ref:

```python
from yoke.ai import build_provider, provider_readiness

readiness = provider_readiness()
ready = {item.provider_name: item for item in readiness if item.ready}
if "zai" not in ready:
    reasons = {item.provider_name: item.reason for item in readiness}
    raise RuntimeError(f"zai is not ready: {reasons.get('zai')}")

provider = build_provider("zai:glm-5.2:none")
```

Completion criterion: the script checks all provider readiness first and uses
`build_provider(...)` instead of hand-written provider configuration.

## One-Off Yoke AI Scripts

Use a one-off script when the user wants a few subagents or a disposable
automation, but the work does not need live Observe inspection.

```python
from pathlib import Path
from pydantic import BaseModel
from yoke.ai import Agent, RunConfig


class ReviewSummary(BaseModel):
    findings: list[str]
    ok: bool


root = Path.cwd()
agent = Agent(provider=provider, config=RunConfig(root=root))
result = agent.prompt("Review src/yoke/foo.py", output_type=ReviewSummary)
assert result.structured is not None
print(result.structured.model_dump_json(indent=2))
```

Patterns:

- Put disposable scripts under ignored local paths such as `.yoke/manual/`.
- Use Pydantic `output_type` for every boundary that downstream code consumes.
- Reuse the same `Agent` object when conversation context should persist.
- Create separate `Agent` objects only when separate memory is intended.

Completion criterion: the script can run once, print or persist its typed
result, and be deleted without affecting the repo.

## Observable Workflows

Use Observe for big tasks that require coordination and time: fan-out reviews,
implementation/reviewer loops, multi-file planning, long merges, handoffs, or
anything the user may want to inspect while it runs.

```python
from yoke.observe import step, workflow


@step
def review_file(path: str) -> FileReview:
    result = reviewer.prompt(f"Review {path}", output_type=FileReview)
    assert result.structured is not None
    return result.structured


with workflow("review-module", root=Path.cwd()) as run:
    plan = planner.prompt("Plan the review.", output_type=ReviewPlan).structured
    assert plan is not None
    reviews = [review_file(path) for path in plan.files]
    final = merger.prompt(str(reviews), output_type=FinalReview)
    print(run.run_id)
```

Completion criterion: Observe state shows meaningful workflow steps, typed
outputs, dependency edges, and a printed run id.

## Condition Loops

When the task says "loop", make it condition-controlled by structured output.
Do not hard-code a fake fixed sequence and call it a loop.

```python
class Decision(BaseModel):
    ok: bool
    next_request: str


for _ in range(5):
    decision = agent.prompt(prompt, output_type=Decision).structured
    assert decision is not None
    if decision.ok:
        break
    prompt = f"Continue from prior context: {decision.next_request}"
```

Completion criterion: the loop exits because `ok` becomes true or because an
explicit guard limit is reached.

## Fan-Out And Merge

For coordinated work:

1. A planning step returns typed work units.
2. Worker steps consume those typed units.
3. A merge step consumes all typed worker outputs.

Completion criterion: the graph reads as `plan -> workers -> merge`, not as a
flat pile of low-level helper calls.

## Follow A Workflow

Use the CLI while the workflow is running:

```bash
yoke observe list --root .
yoke observe state latest --root .
yoke observe watch latest --root .
```

Start the web viewer locally:

```bash
yoke observe serve --root . --host 127.0.0.1 --port 8787
```

Start the web viewer over Tailscale:

```bash
yoke observe serve --root . --host 100.85.95.123 --port 8787
```

Completion criterion: the user has either CLI state output or a browser URL.
For web inspection, the sidebar gives compact node state and `Open details`
shows structured input/output plus agent turns.

## Good Workflow Hygiene

- Use Observe only when inspection is worth the overhead.
- Keep `@step` boundaries meaningful and user-recognizable.
- Keep Pydantic models small and named for domain concepts.
- Print `run.run_id`.
- Keep long-running scripts resumable or easy to rerun.
- Keep generated/manual workflow scripts out of commits unless they are meant
  to become supported examples.

Completion criterion: a maintainer can understand the workflow graph without
reading logs, and can inspect long-running work from CLI or web.
