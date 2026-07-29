---
name: yoke-subagents
description: "Orchestrate multi-agent task workflows with the yoke SDK: research, discovery-driven investigation, fan-out analysis, multi-file implementation, coder/reviewer loops, merge handoffs, or durable role agents."
---

# Yoke Subagent Orchestration

Do not look for a subagent tool. The orchestration script creates SDK `Agent`
instances directly.

Use this skill selectively. It is an orchestration workflow for tasks where
parallel viewpoints, durable roles, or review/merge handoffs are worth the
overhead; it is not the default path for small single-threaded work.

## Core Process

1. Print locally configured/catalog-listed provider status, then call
   `build_builtin_provider(selection)` for every selected provider/model/thinking
   string before writing or launching the orchestrator. This validates local
   selection construction, not remote service reachability.
   When adding or changing a provider, separately probe a representative real
   agent turn with function tools and complete the tool-result round trip.
2. Choose the smallest orchestration shape that fits the request.
3. Prefer public SDK `run_many()` for independent bounded fan-out. For small
   orchestrations (1-4 subagents, no complex state), run the code
   inline without creating a file. For larger or reusable orchestrations, write
   an import-side-effect-free script under `.agents_local/` with an async
   `main()` launched by `asyncio.run(main())`, guarded by
   `if __name__ == "__main__"`.
4. Log phase starts, selected providers, task start/finish/error events,
   artifact paths, and final status to stdout and a `.agents_local/` log file.
5. For file-based orchestrations, run the script with `exec_command`; poll with
   `write_stdin` until it completes. Subagent tasks can take a long time, so
   choose a `yield_time_ms` up to 3,600,000 ms when long waits are useful,
   while keeping progress visible.
6. Read the final handoff and raw JSON artifacts. Verify conflicts or errors
   before trusting the subagent results.
7. The main agent applies final edits, resolves conflicts, runs validation, and
   reports the final outcome.
8. Let `run_many()` close independent one-shot agents. Use `async with agent`
   for sequential or durable roles so provider and tool resources are released.

## Completion Criteria

The orchestration is not complete until all applicable criteria are satisfied:

- Provider status was printed and every selected provider/model/thinking string
  was constructed successfully with `build_builtin_provider(selection)`.
- The orchestrator finished without unhandled exceptions, or every failure is
  captured in the handoff with a clear blocker.
- The final handoff artifact and any raw task JSON were written under
  `.agents_local/`.
- A review or merge pass checked coverage, conflicts, unsupported claims, and
  task errors before the main agent acted on the results.
- Caps were respected unless the user explicitly asked otherwise.
- For implementation work, file ownership was non-overlapping, changed files
  were reported, and the main agent ran final validation.
- Async fan-out used a fresh agent factory, bounded concurrency, stable unique
  task IDs, and inspected every per-item terminal status.

## Provider Selection

Use provider/model/thinking selections as strings:

```text
provider:model:thinking_effort
```
See [`SDK_SURFACE.md`](SDK_SURFACE.md) for imports, provider helper behavior,
capability IDs, durable agent state, and reusable script helpers.

## Orchestration Shapes

1. **Quick audit** — ask 2-4 read-only subagents for independent perspectives
   when full discovery/planning/merge machinery would be too heavy.
2. **Research** — answer an open question with codebase evidence, online
   sources, or both.
3. **Discovery** — find concrete work items when the task boundary is unknown.
4. **Planning** — convert discoveries into bounded, non-overlapping task specs.
5. **Fan-out** — run independent tasks concurrently and collect structured
   evidence, changes, validation, and risks.
6. **Coder/reviewer pairs** — iterate scoped implementation work until a
   reviewer returns `ok`, a max iteration cap is hit, or the main agent must
   intervene.
7. **Review and coverage** — check results against the request and discovery
   outputs for missing coverage, conflicts, and unsupported claims.
8. **Merge handoff** — synthesize a compact report for the main agent with
   findings, changed files, risks, blockers, and next actions.

See [`PATTERNS.md`](PATTERNS.md) for async code templates for every shape. Do not
copy older `ThreadPoolExecutor` or synchronous `worker.prompt(...)` fan-out
patterns into new orchestrators.

## Durable Role Agents

Use durable SDK agents when a role accumulates judgment over multiple turns,
such as reviewer -> main agent fix -> reviewer, planner -> fan-out -> planner,
or merge agent -> conflict resolution -> merge agent.

Bind each long-lived role to its own state file under `.agents_local/`. Validate
that task IDs are unique filename-safe slugs before deriving paths from them:

```python
# inside Agent(...)
state_path=OUTPUT_DIR / f"{task.id}.reviewer.json",
autosave=True,
```

Do not persist throwaway one-shot fan-out agents by default. Persistence is most
useful for roles that preserve prior objections, accepted tradeoffs, review
criteria, and task-specific context across crashes or later continuation.

## Write Safety

Subagents may perform real implementation work when the task can be partitioned
safely and the user has not asked for a read-only audit.

- Assign each implementation subagent an exclusive file or directory scope.
- Require each implementation subagent to report changed files and validation.

## Async Fan-Out

Use `Agent.prompt_async()` for an asyncio-compatible call on one stateful agent.
Concurrent calls on that agent serialize intentionally. Use `run_many()` for
parallel independent tasks because it creates and closes one agent per task,
isolates errors, preserves input order, and aggregates provider-reported usage.
The factory must return a fresh agent for every task and retry attempt; reused
instances are rejected. Factories may be synchronous or asynchronous. Inspect
`progress_errors` as part of the handoff when a progress callback is configured.

Do not add generic agent or batch timeouts to orchestration templates. Agents
can legitimately run for a long time; monitor progress and cancel explicitly
when work is genuinely stalled. If a task has a real domain deadline, remember
that SDK timeouts are cooperative: yoke signals the synchronous runtime and waits
for cleanup, and a non-cooperative dependency can delay return.

## Caps

Use caps by default:

- No more than 16 concurrent subagents unless the user explicitly asks.
- No more than 64 subagent tasks in one script unless the user explicitly asks.
