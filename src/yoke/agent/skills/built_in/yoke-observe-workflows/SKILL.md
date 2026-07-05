---
name: yoke-observe-workflows
description: Use when building or running Yoke SDK workflows that need provider readiness, provider refs, typed Pydantic outputs, condition loops, fan-out/merge graphs, or live Observe CLI/web inspection.
---

# Yoke Observe Workflows

Use this skill to write, run, or inspect a Yoke SDK workflow whose progress
should be visible through typed execution data.

## Setup Gate

Start every workflow script by listing provider readiness for the target
environment, then construct the provider from one qualified ref:

```python
from yoke.ai import build_provider, provider_readiness

readiness = provider_readiness()
ready = {item.provider_name: item for item in readiness if item.ready}
if "zai" not in ready:
    reasons = {item.provider_name: item.reason for item in readiness}
    raise RuntimeError(f"zai is not ready: {reasons.get('zai')}")

provider = build_provider("zai:glm-5.2:none")
```

Completion criterion: the script checks all provider readiness first and builds
the chosen provider with `build_provider(...)`, not hand-written provider
configuration.

## Observable Shape

Wrap the run in `workflow(...)`, model every agent boundary with Pydantic, and
decorate meaningful Python boundaries with `@step`.

```python
from pydantic import BaseModel
from yoke.ai import Agent, RunConfig
from yoke.observe import step, workflow


class ReviewPlan(BaseModel):
    files: list[str]


class FileReview(BaseModel):
    path: str
    findings: list[str]


@step
def review_file(path: str) -> FileReview:
    result = reviewer.prompt(f"Review {path}", output_type=FileReview)
    assert result.structured is not None
    return result.structured


with workflow("review-module", root=Path.cwd()) as run:
    planner = Agent(provider=provider, config=RunConfig(root=Path.cwd()))
    plan = planner.prompt("Plan the review.", output_type=ReviewPlan).structured
    assert plan is not None
    reviews = [review_file(path) for path in plan.files]
```

Completion criterion: the Observe state shows typed step outputs, agent outputs,
and dependency edges between produced Pydantic values and consuming steps.

## Condition Loops

When the task says "loop", make the loop exit from typed state, not from a
fixed prompt count. Reuse one `Agent` instance when the loop is a real
conversation.

```python
class ReviewDecision(BaseModel):
    ok: bool
    findings: list[str]
    next_request: str


agent = Agent(provider=provider, config=RunConfig(root=Path.cwd()))
for _ in range(5):
    decision = agent.prompt(prompt, output_type=ReviewDecision).structured
    assert decision is not None
    if decision.ok:
        break
    prompt = f"Continue from the prior turn: {decision.next_request}"
```

Completion criterion: the loop exits because `ok` is true or an explicit guard
limit is reached. The same `Agent` object is reused when context should persist.

## Fan-Out And Merge

For fan-out, produce typed units from a planning step, pass each unit into a
decorated worker step, then merge the typed worker outputs.

Completion criterion: the graph shows `plan -> worker steps -> merge`, and the
merge step receives a list of typed worker outputs.

## Inspect A Run

Use the CLI while a workflow is running:

```bash
yoke observe list --root .
yoke observe state latest --root .
yoke observe watch latest --root .
```

Expose the browser viewer locally:

```bash
yoke observe serve --root . --host 127.0.0.1 --port 8787
```

Expose it over Tailscale by binding the Tailscale IP:

```bash
yoke observe serve --root . --host 100.85.95.123 --port 8787
```

Completion criterion: `observe list` shows the run id, `observe state` shows
current status, and the web UI shows the live graph. Use the sidebar for a
compact node summary and `Open details` for structured input/output plus agent
turn history.

## Good Workflow Patterns

- Keep workflow scripts disposable: put one-off scripts under ignored local
  paths such as `.yoke/manual/`.
- Keep graph nodes meaningful: decorate business boundaries, not every helper.
- Keep outputs typed: prefer small Pydantic models named for workflow concepts
  like `ReviewPlan`, `LoopResult`, `MergeDecision`, or `ImplementationPlan`.
- Keep loops bounded: every condition-controlled loop needs a max iteration
  guard.
- Keep agents stable: one `Agent` object means one persisted conversation;
  separate objects mean separate memory and separate observed agent state.
- Keep inspection reachable: print `run.run_id`, and tell the user the CLI
  command or server URL that can follow the run.

Completion criterion: a maintainer can rerun the script, inspect it from CLI or
web, and understand the graph from typed outputs without reading logs.
