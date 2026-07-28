# SDK Surface Reference

Use public imports from `yoke.ai`. Do not import implementation modules under
`yoke.ai.sdk.*`.

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

from yoke.ai import Agent, BatchProgress, BatchTask, RunConfig
from yoke.ai import build_builtin_provider, run_many
from yoke.ai import print_builtin_provider_status
```

## Provider Helpers

- `print_builtin_provider_status()` prints locally ready/unavailable providers,
  models, thinking efforts, and selection strings. It is not a remote check.
- `build_builtin_provider(selection)` builds a provider from a
  `provider:model:thinking_effort` string. Omit the selection to use the first
  locally ready default.
- `available_builtin_providers(selections=...)` constructs every ready requested
  selection and returns a mapping for orchestration.
- Provider construction does not prove remote reachability. Provider changes
  need a real turn through any tool-call and tool-result cycle.

## Capability IDs

Prefer capability IDs unless a task needs a concrete tool class. They resolve
through the same provider-aware registry used by the CLI.

- `file.read` reads workspace text.
- `file.write` selects model-appropriate editing tools.
- `file.search` provides `rg` and `fd` when installed, with Python fallbacks.
- `file.extract_context` extracts readable document context.
- `image.attach` is omitted when the provider cannot accept images.
- `mcp` exposes configured MCP discovery and calls.
- `web.fetch` and `web.research` provide network research.
- `shell` provides shell and Python execution.

```python
def coding_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            tools=["file.read", "file.search", "file.write", "shell"],
        ),
    )
```

## Durable Agents

Use durable SDK agents for roles that preserve judgment across turns or process
restarts. Do not persist throwaway fan-out workers.

- `state_path=Path(...)` binds an agent to a versioned snapshot.
- `autosave=True` saves after each successful prompt.
- `agent.save(path=None)` writes state; omit `path` when already bound.
- `Agent.load(path, provider=..., config=...)` restores with fresh dependencies.
- `agent.restore(path)` replaces state while retaining provider/configuration.

```python
reviewer = Agent(
    provider=build_builtin_provider(task.selection),
    config=RunConfig(
        root=Path.cwd(),
        tools=["file.read", "file.search"],
    ),
    state_path=OUTPUT_DIR / f"{task.id}.reviewer.json",
    autosave=True,
)
```

Validate task IDs as unique filename-safe slugs before deriving paths. State
files may contain proprietary prompts, outputs, tool results, and paths.

## Async Agents and Batches

`await agent.prompt_async(...)` mirrors `agent.prompt(...)` and adds `timeout`.
Concurrent calls on one stateful agent serialize. Cancellation and timeouts are
cooperative and wait for cleanup.

Use `run_many()` for independent fan-out. Pass input-ordered `BatchTask` values
and a synchronous or asynchronous factory that creates a fresh `Agent` for each
task and retry. It bounds concurrency, closes agents/providers, isolates errors,
preserves input order, reports progress, and aggregates provider usage.

Inspect every item status and `progress_errors`:

```python
batch = await run_many(
    tasks,
    agent_factory=lambda task: read_only_agent(DEFAULT_SELECTION),
    max_concurrency=8,
    max_attempts=2,
    on_progress=log_progress,
)
for item in batch.items:
    if item.status != "completed":
        LOGGER.error("Task %s failed: %r", item.task.id, item.error)
```

## Shared Helpers

```python
DEFAULT_SELECTION = "zai:glm-5.2:none"  # Select from readiness/user guidance.
MAX_CONCURRENCY = 8
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
    for handler in (
        logging.FileHandler(path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


LOGGER = logging.getLogger("yoke-subagents")


def read_only_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            sys_prompt="Stay read-only and report evidence with file paths.",
            tools=["file.read", "file.search", "file.extract_context"],
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
    return json.dumps(
        value,
        default=lambda item: (
            item.model_dump(mode="json") if isinstance(item, BaseModel) else str(item)
        ),
        indent=2,
    )


async def main() -> None:
    setup_logger()
    print_builtin_provider_status()
    provider = build_builtin_provider(DEFAULT_SELECTION)
    close = getattr(provider, "close", None)
    if callable(close):
        close()
    LOGGER.info("Provider selection constructed: %s", DEFAULT_SELECTION)


if __name__ == "__main__":
    asyncio.run(main())
```
