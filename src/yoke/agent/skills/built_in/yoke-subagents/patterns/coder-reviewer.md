# Coder/reviewer loop

Read [`../COMMON.md`](../COMMON.md) first. Use a durable reviewer when objections,
accepted tradeoffs, or review criteria must survive later turns or interruption.

## Contents

- [Safety invariants](#safety-invariants)
- [Reference loop](#reference-loop)

## Safety invariants

The reviewer must receive the immutable task criteria and inspect the workspace;
coder prose alone is not review evidence.

## Reference loop

```python
from contextlib import AsyncExitStack
from typing import Literal

from pydantic import BaseModel, Field


class PairReview(BaseModel):
    verdict: Literal["ok", "nok"]
    checked_files: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


async def run_coder_reviewer_pair(
    task: TaskSpec,
    user_request: str,
    *,
    run_id: str,
    max_rounds: int = 3,
) -> dict[str, object]:
    validate_slug(task.id, label="task id")
    validate_slug(run_id, label="run id")
    if not task.mutates_workspace:
        raise ValueError("Coder/reviewer requires a mutating task")
    if max_rounds < 1:
        raise ValueError("max_rounds must be positive")
    preflight_selections({task.selection})
    run_dir = OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    task_context = (
        f"Overall request:\n{user_request}\n\n"
        f"Task id: {task.id}\nScope: {task.scope}\n"
        f"Mutates workspace: {task.mutates_workspace}\n\n"
        f"Task instructions:\n{task.prompt}"
    )

    async with AsyncExitStack() as stack:
        coder = await stack.enter_async_context(coding_agent(task.selection))
        reviewer = await stack.enter_async_context(
            Agent(
                provider=build_builtin_provider(task.selection),
                config=RunConfig(
                    root=Path.cwd(),
                    sys_prompt=(
                        "Review the actual workspace and validation evidence. "
                        "Never approve from coder claims alone."
                    ),
                    tools=["file.read", "file.search", "file.extract_context"],
                ),
                state_path=run_dir / f"{task.id}.reviewer.json",
                autosave=True,
            )
        )

        coder_result = await coder.prompt_async(
            "Implement only the assigned task. Re-read current files before each "
            "edit. Report exact changed files and validation.\n\n" + task_context
        )
        coder_result.require_completed()
        coder_output = coder_result.output

        for round_index in range(max_rounds):
            review_result = await reviewer.prompt_async(
                "Review this task against its original criteria. Inspect the "
                "workspace and changed files directly. Return ok only when the "
                "implementation and validation are ready to merge.\n\n"
                f"{task_context}\n\nCoder report:\n{coder_output}",
                output_type=PairReview,
            )
            review_result.require_completed()
            review = review_result.structured
            if review is None:
                raise RuntimeError("Reviewer returned no structured value")
            if review.verdict == "ok" and (
                not review.checked_files or not review.evidence
            ):
                review.verdict = "nok"
                review.feedback.append(
                    "Approval requires checked files and concrete evidence."
                )
            if review.verdict == "ok":
                return {
                    "task": task.id,
                    "status": "accepted",
                    "coder_output": coder_output,
                    "review": review,
                }
            if round_index == max_rounds - 1:
                break
            revision_result = await coder.prompt_async(
                "Re-read the current workspace and revise only this task using "
                "the review evidence. Report changed files and validation.\n\n"
                f"{task_context}\n\nReview:\n{json_payload(review)}"
            )
            revision_result.require_completed()
            coder_output = revision_result.output

        return {
            "task": task.id,
            "status": "needs-main-agent",
            "coder_output": coder_output,
            "review": review,
        }
```

`AsyncExitStack` closes the coder if reviewer construction fails and closes both
roles on every return or exception. Use run-specific directories to avoid state
collisions across concurrent or repeated orchestrations.
