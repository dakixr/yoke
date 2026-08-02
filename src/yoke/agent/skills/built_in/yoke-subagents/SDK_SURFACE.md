# SDK surface reference

## Contents

- [Public imports](#public-imports)
- [Provider helpers](#provider-helpers)
- [Result safety](#result-safety)
- [Structured outputs](#structured-outputs)
- [Durable agents](#durable-agents)
- [Async agents and batches](#async-agents-and-batches)
- [Artifact helpers](#artifact-helpers)

## Public imports

Use public imports from `yoke.ai`; do not import `yoke.ai.sdk.*` implementation
modules.

```python
from yoke.ai import Agent, AgentNotCompletedError, AgentResult
from yoke.ai import BatchProgress, BatchResult, BatchTask, RunConfig
from yoke.ai import build_builtin_provider, run_many
from yoke.ai import discover_capabilities, print_builtin_provider_status
from yoke.ai import to_jsonable, write_json_artifact
```

Read [`CAPABILITIES.md`](CAPABILITIES.md) for capability and discovery examples.
`discover_capabilities(selection=...)` owns and closes a temporary provider;
`discover_capabilities(provider=...)` leaves ownership with the caller.

## Provider helpers

- `print_builtin_provider_status()` prints locally ready/unavailable providers,
  missing configuration, models, reasoning efforts, and copyable selections.
- `build_builtin_provider(selection)` builds a provider from
  `provider:model:thinking-effort`. The string's “thinking effort” maps to the
  Python API's `reasoning_effort` field.
- `available_builtin_providers(selections=...)` builds ready requested
  selections, skipping invalid values.

The SDK-level historical `builtin_*` names include installed custom providers
from `~/.yoke/providers`. The lower-level resolver has a separate genuinely
built-in-only constructor; orchestration scripts should use the public SDK helper.

Status and construction validate local configuration, not remote reachability.
Probe changed provider implementations with a real function-tool call and the
complete tool-result round trip.

Give providers to `Agent` and close the agent. `with Agent(...)`,
`async with Agent(...)`, `Agent.close()`, and `Agent.aclose()` release runtime
tools and close supported providers.

## Result safety

`AgentResult.status` is `"completed"` or `"stopped"`. Never treat output presence
as completion.

```python
result = await agent.prompt_async("Review the implementation.")
result.require_completed()
print(result.output)
```

`result.completed` is the boolean form. `require_completed()` returns the same
result or raises `AgentNotCompletedError`, whose `status` and `output` fields are
available for handoffs. Use the guard at every role boundary.

SDK result and batch value objects are dataclasses, not Pydantic models. Use
`to_jsonable()` or `write_json_artifact()` rather than `.model_dump()` on them.

## Structured outputs

Pass a Pydantic model as `output_type`. `Agent.prompt()` and `prompt_async()` make
up to three internal attempts. On invalid JSON, Yoke adds schema correction
instructions, changes the retry prompt, and does not resend images or image URLs.
If all attempts fail, `StructuredOutputError.output` contains the final raw text.

```python
result = await agent.prompt_async(prompt, output_type=ReviewResult)
result.require_completed()
if result.structured is None:
    raise RuntimeError("Review returned no structured value")
review = result.structured
```

`run_many(max_attempts=N)` retries whole agents outside this internal structured
loop, so one batch item can make up to `3 * N` provider attempts. Keep mutating
batches at one attempt unless retries are explicitly safe.

## Durable agents

- `state_path=...` binds a snapshot. If it already exists, the constructor
  restores it immediately.
- `autosave=True` saves only completed prompts.
- `agent.save(path=None, metadata=None, atomic=True)` saves portable state.
- `Agent.load(path, provider=..., config=..., autosave=False, strict=True)` creates
  an agent with fresh runtime dependencies and restored state.
- `agent.restore(path, strict=True)` replaces state while keeping the current
  provider and `RunConfig`.
- `agent.fork()` does not inherit persistence binding or autosave.

State snapshots can contain prompts, outputs, tool results, paths, and proprietary
data. Use a unique run directory and a distinct validated slug per durable role.
Providers, credentials, tools, and callbacks are not persisted.

```python
reviewer = Agent(
    provider=build_builtin_provider(selection),
    config=RunConfig(
        root=Path.cwd(),
        tools=["file.read", "file.search", "file.extract_context"],
    ),
    state_path=run_dir / f"{task_id}.reviewer.json",
    autosave=True,
)
```

## Async agents and batches

`prompt_async()` serializes concurrent calls on one stateful agent. Its timeout
includes that agent's lock queue, then cooperatively stops the synchronous runtime
and waits for cleanup. It does not promise to interrupt non-cooperative provider
or tool dependencies.

`run_many()`:

- requires non-empty unique task IDs and a fresh agent per task and retry;
- accepts synchronous or asynchronous factories;
- bounds active work and preserves input order;
- isolates errors and returns terminal `completed`, `error`, or `timed_out` items;
- closes every created agent and treats close failure as an attempt failure;
- aggregates provider-reported transcript usage;
- captures callback exceptions in `batch.progress_errors`.

Its per-task timeout begins after the task acquires the batch concurrency slot;
batch semaphore wait is not part of that timeout.

Always inspect every item and `progress_errors`:

```python
batch = await run_many(
    tasks,
    agent_factory=lambda task: read_only_agent(selection),
    max_concurrency=8,
    on_progress=log_progress,
)
require_batch_integrity(batch)
for item in batch.items:
    if item.status != "completed" or item.result is None:
        LOGGER.error("task_error id=%s error=%r", item.task.id, item.error)
        continue
    item.result.require_completed()
```

Cancellation of the outer batch cancels and joins workers. Do not add generic
agent or batch timeouts to templates; use explicit domain deadlines only.

## Artifact helpers

`to_jsonable()` recursively preserves Pydantic models, SDK dataclasses, paths,
dates, enums, exceptions, mappings, and sequences. It raises for unknown or
lossy values rather than silently stringifying them.

`write_json_artifact(path, payload, atomic=True, indent=2)` creates parent
directories, normalizes the payload, and atomically replaces the target by
default. Use it for raw task results, reviews, and handoffs.

```python
raw_path = write_json_artifact(
    Path(".agents_local") / "raw.json",
    {"selection": selection, "batch": batch},
)
```
