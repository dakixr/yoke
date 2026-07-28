---
name: yoke-subagents
description: "Orchestrate multi-agent task workflows with the yoke SDK: research, discovery-driven investigation, fan-out analysis, multi-file implementation, coder/reviewer loops, merge handoffs, or durable role agents."
---

# Yoke Subagent Orchestration

Do not look for a subagent tool. The orchestration script creates SDK `Agent`
instances directly.

Use this skill selectively. Parallel viewpoints, durable roles, or review and
merge handoffs must justify their overhead; keep small single-threaded work in
the main agent.

## Core Process

1. Call `provider_status(selection)` and `build_provider(selection)` for every
   selected provider/model/thinking string before launching the orchestrator.
   Construction validates local configuration, not remote reachability.
2. Choose the smallest orchestration shape that fits the request.
3. Prefer public SDK `run_many()` for bounded independent fan-out. For 1-4
   agents with no reusable state, run async orchestration inline. For a
   larger or reusable workflow, write an import-side-effect-free script under
   `.agents_local/` with an async `main()` guarded by
   `if __name__ == "__main__"`.
4. Log phase starts, providers, task outcomes, artifact paths, and final status
   to stdout and `.agents_local/yoke_subagents.log`.
5. Run file-based orchestrators with `exec_command`; use `write_stdin` to poll
   long-running sessions while keeping progress visible.
6. Read the handoff and raw JSON artifacts. Check every task error, conflict,
   and unsupported claim before using the results.
7. The main agent owns final edits, conflict resolution, validation, and the
   user-facing outcome.
8. Let `run_many()` close one-shot agents. Use `async with agent` or
   `await agent.aclose()` for sequential/durable roles; closing an SDK agent
   releases its provider after the final shared fork closes.

## Completion Criteria

- Every provider selection was readiness-checked and constructed locally.
- The orchestrator completed, or its handoff captures every failure and blocker.
- File-based workflows wrote the final handoff and raw task JSON beneath
  `.agents_local/`.
- A review pass checked coverage, conflicts, unsupported claims, and task errors.
- Concurrency and task caps were respected unless the user changed them.
- Implementation workers had non-overlapping ownership, reported changed files,
  and the main agent ran final validation.
- Fan-out created a fresh agent and provider per task, bounded concurrency, used
  stable unique task IDs, and inspected every terminal result.

## Provider Selection

Use `provider:model:thinking_effort` strings accepted by `build_provider()`.
See [`SDK_SURFACE.md`](SDK_SURFACE.md) for imports, capability configuration,
provider ownership, state capture, and reusable helpers.

## Orchestration Shapes

1. **Quick audit** — 2-4 read-only agents provide independent perspectives.
2. **Research** — gather codebase evidence, online evidence, or both.
3. **Discovery** — identify concrete work items when scope is unknown.
4. **Planning** — turn discoveries into bounded, non-overlapping task specs.
5. **Fan-out** — run independent work concurrently and collect typed results.
6. **Coder/reviewer pairs** — iterate until approval, a cap, or intervention.
7. **Review and coverage** — check request coverage and result consistency.
8. **Merge handoff** — synthesize findings, changes, risks, and next actions.

See [`PATTERNS.md`](PATTERNS.md) for async templates. Use
`Agent.prompt_async()` rather than rebuilding thread-pool wrappers around
`Agent.prompt()`.

## Durable Role Agents

For a role that must survive a process restart, bind `state_path=` and enable
`autosave=True`, or call `save()` explicitly. Resume with `Agent.load()` or
replace an agent's state with `restore()`. Use one file per role after validating
the task ID as a unique filename-safe slug. State files may contain proprietary
prompts, outputs, tool results, and paths.

Do not persist throwaway fan-out agents. Persistence is useful when a reviewer,
planner, or merger must retain prior objections and accepted tradeoffs.

## Write Safety

Subagents may implement changes when the request authorizes implementation and
the work partitions safely.

- Give each worker exclusive file or directory ownership.
- Require changed-file and validation reports.
- Keep review agents read-only.
- Leave cross-scope edits and conflict resolution to the main agent.

## Async Fan-Out

Concurrent `prompt_async()` calls on one stateful agent serialize intentionally.
Use `run_many()` for parallel independent work. Its factory must return a fresh
agent for each task/retry; inspect every item status and `progress_errors` so a
successful aggregate cannot hide an individual failure.

Do not add generic timeouts. Agents can legitimately run for a long time;
monitor progress and cancel explicitly when work is genuinely stalled. A real
domain deadline may use `timeout=`, but cancellation is cooperative and a
non-cooperative dependency can delay cleanup.

## Caps

- No more than 16 concurrent agents unless the user explicitly asks.
- No more than 64 tasks in one orchestrator unless the user explicitly asks.
