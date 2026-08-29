# SDK Surface Reference

Use public SDK imports in orchestration scripts. Do not import implementation
modules under `yoke.ai.sdk.*`; those are behind the stable public
surfaces: `yoke.ai`, `yoke.ai.types`,
`yoke.ai.skills`, `yoke.ai.providers`, and
`yoke.ai.utils`.

## Common Imports

```python
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from yoke.ai import Agent, BatchProgress, BatchTask
from yoke.ai import CompositeObserver, ConsoleObserver, JsonlObserver
from yoke.ai import RunConfig
from yoke.ai import available_builtin_providers
from yoke.ai import build_builtin_provider, run_many
from yoke.ai.utils import print_builtin_provider_status
```

## Provider Helpers

- `print_builtin_provider_status()` is an agent-facing context-gathering helper,
  not an orchestration-script API. Run it before authoring an orchestrator to
  print locally ready/unavailable providers, missing required environment
  variables, models, thinking efforts, and copy-pasteable selection strings. It
  is not a remote health check and takes no selection argument.
- `build_builtin_provider(selection)` builds one provider from a
  `provider:model:thinking_effort` string using environment-backed config. Call
  it for every selected string to validate local model and effort selection.
  Providers created only for validation still own resources. Give each one to a
  short-lived `Agent` and close the agent after validation.
- Provider construction is not a remote health check. Provider implementation
  changes require a real probe with function tools through the full tool-call
  and tool-result cycle.
- `available_builtin_providers(selections=...)` builds every ready requested
  provider selection and returns a mapping usable by the orchestrator. Treat
  those provider instances as owned resources. Hand each one to an `Agent` and
  close its owning agent when finished.

Custom providers from `~/.yoke/providers` also appear in these helpers when
installed.

## Capability IDs

Prefer capability IDs unless a task truly needs a concrete tool class.
Capabilities are provider/model-aware and resolve to concrete tools through the
same registry used by the CLI.

- `file.read` for text and best-effort document/image reading; it registers
  both concrete reading tools.
- `file.write` for model-aware file modification.
- `file.search` for ripgrep-style workspace search.
- `image.attach` for image attachment; it resolves to no tool when the provider
  cannot accept images.
- `image.generate` for Codex-hosted image generation; it resolves to no tool
  for providers without that API.
- `mcp` for configured MCP server discovery and calls; close agents to release
  MCP resources.
- `web.fetch`, `web.search`, and `web.research` for network research. Codex
  research uses hosted Responses web search; other providers retain Yoke's
  bounded local search/fetch/synthesis workflow.
- `shell` for shell and Python execution.

Example scoped construction:

```python
def coding_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            tools=["file.read", "file.search", "file.write"],
        ),
    )
```

## Durable Agents

SDK `Agent` instances can persist portable conversation state to a JSON file.
Use this for long-lived roles, not throwaway one-shot workers.

- `state_path=Path(...)` binds an agent to a durable state file.
- `autosave=True` saves after each successful `prompt()` call.
- `agent.save(path=None)` writes the current state; omit `path` when the agent
  already has a bound `state_path`.
- `Agent.load(path, provider=..., config=...)` resumes state with fresh runtime
  dependencies. Providers, credentials, tools, and callbacks are not stored in
  the state file.
- `agent.restore(path)` replaces an existing agent's state while keeping its
  current provider and `RunConfig`.

Example durable reviewer:

```python
reviewer = Agent(
    provider=build_builtin_provider(task.selection),
    config=RunConfig(root=Path.cwd(), tools=["file.read", "file.search"]),
    state_path=OUTPUT_DIR / f"{task.id}.reviewer.json",
    autosave=True,
)
```

Use separate state files per durable role, such as
`{task.id}.planner.json`, `{task.id}.reviewer.json`, or
`{task.id}.merge.json`, only after validating task IDs as unique filename-safe
slugs. Treat state files as sensitive because they can contain prompts, outputs,
tool results, paths, and proprietary data.

## Async Agents and Batches

`await agent.prompt_async(...)` mirrors `agent.prompt(...)` and adds an optional
`timeout`. Concurrent calls on one stateful agent serialize. Cancellation and
timeouts signal the synchronous runtime cooperatively and propagate to the async
caller immediately. The synchronous worker may continue in the background until
it observes that signal. `Agent.close()` waits for active work before releasing
resources. Timeout includes queue wait; provider and tool timeouts remain
necessary for non-cooperative blocking dependencies.

`RunConfig` has no agent iteration-limit setting. Use `stop_requested` for
explicit cooperative cancellation, or the per-call `prompt_async(...,
timeout=...)` option when a duration bound is needed.

Use `run_many()` for independent fan-out. Pass input-ordered `BatchTask` values
and a synchronous or asynchronous `agent_factory(task)` that creates a fresh
agent. The helper bounds concurrency, closes every created agent, isolates task
errors, preserves input order, emits optional completion progress, and
aggregates available provider-reported usage.
Factories that reuse an agent instance are rejected because each task and retry
attempt must own isolated mutable state and resources. Fresh agents must also
use fresh provider instances; `run_many()` rejects shared provider identities.

When configured, progress callback errors do not abort tasks; inspect
`batch.progress_errors` before writing the final handoff.

Pass `observer=ConsoleObserver("actions")` to direct prompts or `run_many()` for
live commentary and compact tool-call signatures. For reusable orchestrations,
combine console output with a full structured trace:

```python
observer = CompositeObserver(
    ConsoleObserver("actions"),
    JsonlObserver(Path(".agents_local/yoke_subagents.trace.jsonl"), "full"),
)
```

The detail levels are `quiet`, `messages`, `actions`, and `full`. Batch events
include task IDs and retry-attempt numbers. Built-in adapters redact
credential-like argument keys, but full traces can still contain sensitive
prompts, paths, and tool results.

```python
async def fan_out(tasks: list[BatchTask]) -> None:
    batch = await run_many(
        tasks,
        agent_factory=lambda task: read_only_agent(DEFAULT_SELECTION),
        max_concurrency=8,
        max_attempts=2,
        on_progress=log_progress,
        observer=observer,
    )
    for item in batch.items:
        if item.status != "completed":
            LOGGER.error("Task %s failed: %r", item.task.id, item.error)
    for error in batch.progress_errors:
        LOGGER.error("Progress callback failed: %r", error)
```

## Shared Helpers

```python
DEFAULT_SELECTION = "codex:gpt-5.6-sol:medium"  # Select for the task/user guidance.
MAX_CONCURRENCY = 8  # Pool size; the skill's 16-agent cap is the ceiling.
OUTPUT_DIR = Path(".agents_local")
LOG_PATH = OUTPUT_DIR / "yoke_subagents.log"


def setup_logger(path: Path = LOG_PATH) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("yoke-subagents")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


LOGGER: logging.Logger = logging.getLogger("yoke-subagents")


def agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(provider=build_builtin_provider(selection))


def read_only_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            sys_prompt="Stay read-only and report evidence with file paths.",
            tools=["file.read", "file.search"],
        ),
    )


def log_progress(progress: BatchProgress) -> None:
    LOGGER.info(
        "Task finish id=%s status=%s progress=%d/%d attempts=%d",
        progress.task_id,
        progress.status,
        progress.completed,
        progress.total,
        progress.attempts,
    )


def json_payload(value: object) -> str:
    def serialize(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        return str(item)

    return json.dumps(value, default=serialize, indent=2)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOGGER.info("Wrote JSON artifact: %s", path)


async def main() -> None:
    setup_logger()
    validation_agent = Agent(
        provider=build_builtin_provider(DEFAULT_SELECTION),
        config=RunConfig(root=Path.cwd(), tools=[]),
    )
    validation_agent.close()
    LOGGER.info("Provider selection constructed: %s", DEFAULT_SELECTION)
    # Run the selected async orchestration shape and write its handoff.


if __name__ == "__main__":
    asyncio.run(main())
```
