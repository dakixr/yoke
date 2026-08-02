# Orchestration pattern index

This file preserves the former entry point while keeping branch details
progressively disclosed. Read [`COMMON.md`](COMMON.md) for shared scaffolding,
then read only the pattern matching the task:

- [Quick audit and research](patterns/audit-research.md)
- [Discovery, planning, and fan-out](patterns/pipeline.md)
- [Coder/reviewer loops](patterns/coder-reviewer.md)
- [Coverage review and merge](patterns/review-merge.md)

All patterns use async SDK entry points, fresh agents for independent work,
`AgentResult.require_completed()` at role boundaries, explicit batch integrity
checks, and atomic artifacts. Treat them as composable reference implementations,
not mandatory all-in-one pipelines.
