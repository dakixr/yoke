# Shared orchestration scaffolding

Use this reference for reusable file-based orchestrators. Branch pattern files
assume these imports and helpers.

## Contents

- [Shared imports and helpers](#shared-imports-and-helpers)
- [Async entry point](#async-entry-point)

## Shared imports and helpers

```python
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from yoke.ai import Agent, BatchProgress, BatchResult, RunConfig
from yoke.ai import build_builtin_provider
from yoke.ai import print_builtin_provider_status, to_jsonable, write_json_artifact

DEFAULT_SELECTION = "codex:gpt-5.6-luna:high"
MAX_CONCURRENCY = 8
OUTPUT_DIR = Path(".agents_local")
LOG_PATH = OUTPUT_DIR / "yoke_subagents.log"
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TaskSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scope: str
    prompt: str
    selection: str = DEFAULT_SELECTION
    mutates_workspace: bool = False


def setup_logger(path: Path = LOG_PATH) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("yoke-subagents")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
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


def validate_slug(value: str, *, label: str = "id") -> str:
    if not SLUG.fullmatch(value):
        raise ValueError(f"{label} must be a filename-safe lowercase slug: {value!r}")
    return value


def preflight_selections(selections: set[str]) -> None:
    print_builtin_provider_status()
    for selection in sorted(selections):
        validator = Agent(
            provider=build_builtin_provider(selection),
            config=RunConfig(
                root=Path.cwd(),
                tools=(),
                include_agents_file=False,
            ),
        )
        validator.close()
        LOGGER.info("provider_preflight selection=%s", selection)


def read_only_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            sys_prompt="Stay read-only and cite concrete evidence.",
            tools=["file.read", "file.search", "file.extract_context"],
        ),
    )


def coding_agent(selection: str = DEFAULT_SELECTION) -> Agent:
    return Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(
            root=Path.cwd(),
            tools=[
                "image.attach",
                "file.extract_context",
                "file.search",
                "file.read",
                "web.fetch",
                "web.research",
                "file.write",
                "shell",
            ],
        ),
    )


def log_progress(progress: BatchProgress) -> None:
    LOGGER.info(
        "task_finish id=%s status=%s progress=%d/%d attempts=%d",
        progress.task_id,
        progress.status,
        progress.completed,
        progress.total,
        progress.attempts,
    )


def require_batch_integrity(batch: BatchResult) -> None:
    if batch.progress_errors:
        raise ExceptionGroup("Batch progress callbacks failed", batch.progress_errors)


def json_payload(value: object) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)


def write_artifact(name: str, payload: object) -> Path:
    path = write_json_artifact(OUTPUT_DIR / name, payload)
    LOGGER.info("artifact=%s", path)
    return path
```

For a script, set up logging, preflight every distinct selection, log task starts
inside the agent factory, inspect every terminal item, call
`require_batch_integrity()`, and write both raw results and the final handoff.

## Async entry point

Use an import-side-effect-free async `main()`:

```python
async def main() -> None:
    global LOGGER
    LOGGER = setup_logger()
    preflight_selections({DEFAULT_SELECTION})
    # Run the selected branch and write artifacts.


if __name__ == "__main__":
    asyncio.run(main())
```
