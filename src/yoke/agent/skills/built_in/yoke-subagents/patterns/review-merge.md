# Coverage review and merge handoff

Read [`../COMMON.md`](../COMMON.md) first. Run review after fan-out and before the
main agent acts on worker claims.

```python
from pydantic import BaseModel, Field


class ReviewResult(BaseModel):
    passed: bool
    missing_coverage: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    task_errors: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


async def review_results(
    user_request: str,
    discoveries: object,
    results: object,
) -> ReviewResult:
    async with read_only_agent() as worker:
        result = await worker.prompt_async(
            "Review coverage, correctness, conflicts, unsupported claims, and "
            "every terminal task error. Compare results with discovery and the "
            "original request. Return only structured data.\n\n"
            f"Request:\n{user_request}\n\n"
            f"Discoveries:\n{json_payload(discoveries)}\n\n"
            f"Results:\n{json_payload(results)}",
            output_type=ReviewResult,
        )
    result.require_completed()
    if result.structured is None:
        raise RuntimeError("Review returned no structured value")
    return result.structured


async def merge_handoff(
    user_request: str,
    payload: object,
    review: ReviewResult,
) -> str:
    async with read_only_agent() as worker:
        result = await worker.prompt_async(
            "Produce the final main-agent handoff. Include validated findings, "
            "changed files, conflicts, task errors, risks, and next actions. Do "
            "not repeat rejected or unsupported claims.\n\n"
            f"Request:\n{user_request}\n\n"
            f"Review:\n{json_payload(review)}\n\n"
            f"Payload:\n{json_payload(payload)}"
        )
    result.require_completed()
    return result.output
```

Write both artifacts atomically:

```python
review = await review_results(user_request, discoveries, results)
write_artifact("review.json", review)
handoff = await merge_handoff(user_request, results, review)
write_artifact("handoff.json", {"handoff": handoff, "review": review})
```

The main agent must still inspect raw task artifacts, reconcile changed-file
overlap, apply integration changes, and run repository validation. A polished
handoff is not evidence that its underlying task results succeeded.
