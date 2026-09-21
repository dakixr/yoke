---
name: yoke-subagents
description: Delegate work through Yoke SDK agents. Use for one delegated question or follow-up conversation, parallel tasks, or persistent roles. Use yoke-sessions for separate CLI conversations.
---

# Yoke subagents

Create SDK `Agent` instances through Python, rather than looking for a subagent
tool. Keep small single-threaded work in the parent. Use `yoke-sessions` for a
separate CLI process, saved CLI conversation, or interactive terminal task.

## Choose a workflow

| Need | Execution |
| --- | --- |
| A small task the parent can finish directly | Stay in the parent; skip orchestration setup. |
| One delegated question or follow-up conversation | One `Agent`, with sequential `prompt_async()` calls. |
| Independent audits, research questions, or partitioned implementation | `run_many()` with a fresh agent and provider per attempt. |
| One role that must remember across processes | One `Agent` with `state_path` and `autosave=True`. |
| Parent implementation with repeated external review | One reviewer `Agent`; reuse it sequentially or persist it when its judgment must survive a process. |
| Both implementation and repeated review need delegation | One coder/reviewer pair per exclusive scope. |

Start with the small, self-contained examples in [PATTERNS.md](PATTERNS.md).
Read [SDK_SURFACE.md](SDK_SURFACE.md) before using unfamiliar provider,
capability, persistence, or cancellation options. Adapt examples to the task;
discovery, planning, and merge agents are optional, not mandatory stages.
For detached work or reusable workers, read the
[usage attribution guidance](SDK_SURFACE.md#usage-attribution).

## Execute

1. Gather local provider choices before writing the orchestrator:
   `from yoke.ai.utils import print_builtin_provider_status; print_builtin_provider_status()`.
   Check requested model and thinking effort against the advertised catalog,
   then construct with `build_builtin_provider(selection)` when creating each
   worker. A separate preflight agent is optional; close it if you create one.
   Construction can silently replace an invalid effort with the model default.
   These checks do not prove remote service health.
2. Give every worker a task contract: objective, source paths, acceptance
   criteria, read-only or owned write paths, validation owner, and dependencies
   when present. A short prompt is enough for simple work; JSON is optional.
   Pass the same contract to its reviewer. Use unique filename-safe IDs for batch
   items and names derived into artifacts or state paths.
   Choose local, web, or mixed research explicitly; local-only tasks get no web tools.
3. Set `RunConfig(root=..., tools=...)` explicitly. Start audits with
   `file.read` and `file.search`. Add writes, shell, or network capabilities only
   for assigned work that needs them. The examples leave test execution to the parent.
4. Use a small guarded Python file under `.agents_local/` for workers that call
   tools. Yoke spawns tool processes that re-import the launcher; stdin scripts
   can fail even when their Python is valid. An existing import-safe host or
   tool-free inline call needs no extra file. Keep setup inside `main()`.
5. For small jobs, print or consume `result.output` directly so the final answer
   is lossless. Add `ConsoleObserver("actions")` when live tool visibility matters;
   its rendered messages are previews and may be truncated. A durable role only
   adds its state file. For multi-stage orchestration, retain raw results, a
   handoff, and a full `JsonlObserver` trace under `.agents_local/<run-id>/`. Add
   a phase log when it helps diagnose the run. Traces and snapshots can contain
   sensitive content despite redaction.
6. Run with the repository's Python environment using `command_exec`. Prefer
   `argv` when no shell syntax is needed. Native Yoke and MCP both use
   `process_read` to wait, `process_input` for nonempty input, and
   `process_cancel` to stop the owned process tree. Copy each returned opaque
   cursor unchanged beside its process `session_id`, and collect remaining output
   after exit. For background launch, longer follow-up waits, or multiple
   processes, read the [process guidance](SDK_SURFACE.md#process-tools-for-launching-workers).
7. Inspect every terminal result, blocker, and `progress_errors` when configured.
   Let `run_many()` close one-shot agents. Use nested `async with` blocks for
   roles so earlier agents close even if a later constructor fails.

## Implementation and state

Assign non-overlapping write paths before dispatch. `RunConfig.root` is a path
base, not a sandbox; prompts do not enforce filesystem ownership. Compare actual
changes with assigned paths afterward. Use isolated worktrees or stronger
isolation when shared-workspace writes cannot be coordinated safely.

Keep write-capable batches at `max_attempts=1` unless retries have explicit
recovery or are idempotent. This does not guarantee exactly-once work:
structured-output correction can re-enter the agent even within one batch attempt.

For persistence, start with the one-role example in [PATTERNS.md](PATTERNS.md).
Existing state files load automatically. Reuse one path for the same intended
role, choose a new path for unrelated work, and keep one writer per state file.
Read [SDK_SURFACE.md](SDK_SURFACE.md) for interruption and restore caveats.

## Completion gate

Account for every dispatched task, including failed or blocked work. Check
evidence, missing coverage, and conflicting findings before acting. For code,
report actual changed paths and the parent's executed validation with outcomes.
Review acceptance applies only to the revision inspected. At the review cap,
return unresolved findings without making another unreviewed edit. Partial
results remain partial even when the orchestration process exits successfully.

Default policy is at most 16 concurrent subagents and 64 dispatched tasks per
orchestration, unless the user requests otherwise. These are skill defaults,
not SDK limits. Monitor progress instead of adding arbitrary timeouts; cancel
stalled work explicitly. Read the cancellation caveats before using a real deadline.
