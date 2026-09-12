"""Implement one scoped task with a persistent coder and read-only reviewer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Literal

# Multiprocessing also imports this file as __mp_main__ when spawning tool workers.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "scripts"

from pydantic import BaseModel

from yoke.ai import Agent, RunConfig, build_builtin_provider

from .orchestration_support import (
    TaskSpec,
    finish,
    json_payload,
    log_phase,
    observer_for,
)
from .orchestration_support import parser, preflight, prepare_run, write_json


class Review(BaseModel):
    """Approval is code review, not a substitute for the parent's final validation."""

    verdict: Literal["ok", "nok"]
    evidence: list[str]
    feedback: list[str]
    risks: list[str]


def role_agent(
    role: str, task: TaskSpec, root: Path, selection: str, run_dir: Path
) -> Agent:
    """Each role owns its provider, tools, snapshot, and session identity."""
    tools = ["file.read", "file.search"]
    if role == "coder":
        tools.append("file.write")
    return Agent(
        provider=build_builtin_provider(
            selection, session_id=f"{run_dir.name}-{task.id}-{role}"
        ),
        config=RunConfig(root=root, tools=tools),
        state_path=run_dir / f"{task.id}.{role}.json",
        autosave=True,
        observer=observer_for(run_dir, label=role),
    )


async def review_pair(
    task: TaskSpec,
    *,
    root: Path,
    selection: str,
    run_dir: Path,
    max_reviews: int = 3,
    resume: bool = False,
) -> dict[str, object]:
    """Every returned implementation has a review, unless an explicit error interrupted it."""
    if not 1 <= max_reviews <= 3:
        raise ValueError("This example allows 1-3 reviews per invocation")
    manifest = {
        "task": task.model_dump(),
        "root": str(root.resolve()),
        "selection": selection,
    }
    manifest_path = run_dir / "contract.json"
    if resume:
        if (
            not manifest_path.exists()
            or json.loads(manifest_path.read_text()) != manifest
        ):
            raise ValueError("Resume requires the same task, root, and selection")
    else:
        if manifest_path.exists() or any(run_dir.glob(f"{task.id}.*.json")):
            raise ValueError("Existing role state requires an explicit resume")
        write_json(manifest_path, manifest)

    history: list[dict[str, object]] = []
    payload: dict[str, object] = {
        "status": "needs_main_agent",
        "task": task,
        "selection": selection,
        "history": history,
        "validation_owner": "parent",
    }
    contract = task.model_dump_json(indent=2)
    instruction = (
        "Inspect current files and any restored context. Implement only the owned scope. "
        "Report actual changed paths and suggested validation commands. "
        "You have no shell; the parent executes validation.\nContract:\n" + contract
    )
    try:
        # Enter the coder's context before constructing the reviewer.
        async with role_agent("coder", task, root, selection, run_dir) as coder:
            async with role_agent(
                "reviewer", task, root, selection, run_dir
            ) as reviewer:
                for iteration in range(1, max_reviews + 1):
                    payload["latest_output_reviewed"] = False
                    result = await coder.prompt_async(instruction)
                    payload["latest_output"] = result.output
                    review = await reviewer.prompt_async(
                        "Inspect the actual scoped files against every acceptance criterion. "
                        "Use the coder report as a lead, not evidence. Return nok for defects "
                        "or unchecked code criteria. Report validation still owed by the parent.\n"
                        f"Contract:\n{contract}\nCoder report:\n{result.output}",
                        output_type=Review,
                    )
                    if review.structured is None:
                        raise RuntimeError("Reviewer returned no structured verdict")
                    verdict = review.structured
                    history.append(
                        {
                            "iteration": iteration,
                            "output": result.output,
                            "review": verdict,
                        }
                    )
                    payload["latest_output_reviewed"] = True
                    write_json(run_dir / "review-history.json", history)
                    if verdict.verdict == "ok":
                        payload["status"] = "accepted"
                        break
                    # A final nok ends the loop, without producing an unreviewed revision.
                    instruction = (
                        f"Revise only the owned scope.\nContract:\n{contract}\n"
                        f"Review:\n{json_payload(verdict)}"
                    )
    except Exception as error:
        payload.update(status="error", error=repr(error))
    return payload


async def main() -> int:
    """Start a new run or explicitly resume a matching role contract."""
    cli = parser(__doc__ or "Coder/reviewer pair", "--task")
    cli.add_argument("--max-reviews", type=int, default=3)
    cli.add_argument("--resume", action="store_true")
    args = cli.parse_args()
    root = args.root.resolve()
    run_dir = prepare_run(root, args.run_id, resume=args.resume)
    try:
        task = TaskSpec.model_validate_json(args.task.read_text())
        await preflight(args.selection, root)
        log_phase(
            run_dir,
            f"Review pair: {task.id}; selection: {args.selection}; resume: {args.resume}",
        )
        payload = await review_pair(
            task,
            root=root,
            selection=args.selection,
            run_dir=run_dir,
            max_reviews=args.max_reviews,
            resume=args.resume,
        )
    except Exception as error:
        payload = {"status": "error", "error": repr(error)}
        if args.resume:
            # Rejected input/preflight must not erase the last valid run's handoff.
            write_json(run_dir / "resume-error.json", payload)
            log_phase(run_dir, f"Resume rejected; previous results retained: {error}")
            return 1
    return finish(run_dir, payload)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
