"""Shared input, preflight, and artifact code for the two skill examples."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from yoke.ai import Agent, CompositeObserver, ConsoleObserver, JsonlObserver, RunConfig
from yoke.ai import build_builtin_provider
from yoke.ai.utils import builtin_provider_status

SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class TaskSpec(BaseModel):
    """The same task contract goes to the worker and its reviewer."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=SLUG)
    request: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    validation: str = "The parent runs final validation; report suggested commands."
    mode: Literal["codebase", "web", "mixed"] = "codebase"


def read_tools(mode: str) -> list[str]:
    """Local-only tasks receive no web, shell, or write capabilities."""
    if mode not in {"codebase", "web", "mixed"}:
        raise ValueError(f"Unknown research mode: {mode}")
    tools = ["file.read", "file.search"] if mode != "web" else []
    if mode != "codebase":
        tools += ["web.fetch", "web.search", "web.research"]
    return tools


async def preflight(selection: str, root: Path) -> None:
    """Reject catalog mismatches, then construct and release a local provider."""
    parts = selection.split(":")
    if len(parts) not in {2, 3} or any(not part for part in parts):
        raise ValueError("Use provider:model or provider:model:thinking_effort")
    status = next(
        (item for item in builtin_provider_status() if item.name == parts[0]), None
    )
    if status is None or not status.ready:
        raise ValueError(f"Provider is not locally ready: {parts[0]}")
    model = next(
        (item.model for item in status.models if item.model.id == parts[1]), None
    )
    if model is None:
        raise ValueError(f"Model is not in the advertised catalog: {parts[1]}")
    if len(parts) == 3 and parts[2] not in model.thinking_levels:
        raise ValueError(f"Unsupported thinking effort: {parts[2]}")
    async with Agent(
        provider=build_builtin_provider(selection),
        config=RunConfig(root=root, tools=[], include_agents_file=False),
    ):
        pass


def json_payload(value: object) -> str:
    """Serialize models and paths; reject unsupported values instead of stringifying."""
    return TypeAdapter(object).dump_json(value, indent=2).decode("utf-8")


def write_json(path: Path, value: object) -> None:
    """Serialize before touching the destination."""
    encoded = json_payload(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded + "\n", encoding="utf-8")
    temporary.replace(path)


def observer_for(run_dir: Path, *, label: str | None = None) -> CompositeObserver:
    """Keep concise activity separate from the full, sensitive trace."""
    return CompositeObserver(
        ConsoleObserver("actions", label=label),
        JsonlObserver(run_dir / "trace.jsonl", "full", label=label),
    )


def parser(description: str, input_flag: str) -> argparse.ArgumentParser:
    """Build the common CLI arguments without reading files or starting agents."""
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--selection", required=True)
    result.add_argument(input_flag, type=Path, required=True)
    result.add_argument("--run-id", default=uuid4().hex)
    return result


def prepare_run(root: Path, run_id: str, *, resume: bool = False) -> Path:
    """Use a new namespace unless the caller explicitly requests a resume."""
    if not root.is_dir():
        raise ValueError(f"Repository root does not exist: {root}")
    if re.fullmatch(SLUG, run_id) is None:
        raise ValueError("run-id must be a filename-safe slug")
    run_dir = root.resolve() / ".agents_local" / run_id
    if resume and not run_dir.is_dir():
        raise ValueError("Cannot resume a missing run directory")
    run_dir.mkdir(parents=True, exist_ok=resume)
    return run_dir


def finish(run_dir: Path, payload: Mapping[str, object]) -> int:
    """Retain the complete outcome, including failures, before returning an exit code."""
    write_json(run_dir / "results.json", payload)
    status = str(payload["status"])
    (run_dir / "handoff.md").write_text(
        f"# Orchestration handoff\n\nStatus: {status}\n\n"
        "Read results.json for every task, error, review, and validation limitation.\n"
        "The parent must check evidence, coverage, changed paths, and final validation.\n",
        encoding="utf-8",
    )
    log_phase(run_dir, f"Finished: {status}; artifacts: {run_dir}")
    return 0 if status in {"completed", "accepted"} else 1


def log_phase(run_dir: Path, message: str) -> None:
    """Keep script phase messages out of the agent trace."""
    with (run_dir / "run.log").open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")
    print(message, flush=True)
