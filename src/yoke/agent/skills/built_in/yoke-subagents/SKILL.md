---
name: yoke-subagents
description: "Orchestrate Yoke SDK agents for bounded fan-out, research, discovery, implementation, durable review loops, and merge handoffs. Use when parallel viewpoints, isolated workers, provider-aware capabilities, or retained role context justify orchestration overhead."
---

# Yoke Subagent Orchestration

Create SDK `Agent` instances directly. Do not look for a subagent tool.

## Core process

1. Print provider status and construct every selected
   `provider:model:thinking-effort` before launching work. Construction validates
   local configuration, not remote reachability. When provider behavior changed,
   also complete one real function-tool and tool-result round trip.
2. Choose the smallest useful shape. Keep 1-4 simple workers inline; place larger,
   reusable, durable, or multi-phase workflows under `.agents_local/`.
3. List the stable capability IDs the task requires, then inspect their
   provider-resolved tools before launching work.
4. Bound independent work with `run_many()`. Give every task a stable unique slug
   and every attempt a fresh agent. Use a single durable agent only when later
   turns need its accumulated judgment.
5. Treat every boundary as typed: call `result.require_completed()`, inspect every
   batch item status, and surface `batch.progress_errors` before trusting output.
6. For writes, assign exclusive scopes. Default mutating tasks to one attempt;
   enable retries only with an explicit idempotency and partial-write policy.
7. Write raw results and the final handoff atomically under `.agents_local/`.
   Log phase, selection, task start/finish/error, artifact path, and final status.
8. Run a separate coverage/review pass. The main agent resolves conflicts, applies
   final integration changes, and runs repository validation.

Each step is complete only when its stated evidence exists in logs or artifacts.

## Progressive references

Load only the references needed for the selected branch:

- Read [`CAPABILITIES.md`](CAPABILITIES.md) when choosing tool access or diagnosing
  provider/model-aware availability.
- Read [`SDK_SURFACE.md`](SDK_SURFACE.md) when using provider helpers, result
  status, structured output, durability, batching, or artifact APIs.
- Read [`COMMON.md`](COMMON.md) before writing a reusable file-based orchestrator.
- Read [`patterns/audit-research.md`](patterns/audit-research.md) for quick audits
  or code/web research.
- Read [`patterns/pipeline.md`](patterns/pipeline.md) for
  discovery → planning → fan-out pipelines.
- Read [`patterns/coder-reviewer.md`](patterns/coder-reviewer.md) for mutating
  coder/reviewer loops or durable review roles.
- Read [`patterns/review-merge.md`](patterns/review-merge.md) for coverage review
  and final handoffs.
- [`PATTERNS.md`](PATTERNS.md) is a compatibility index for the branch files.

## Shape selection

- **Quick audit:** 2-4 independent read-only perspectives.
- **Research:** codebase, web, or configured-integration evidence gathering.
- **Discovery:** one stateful role identifies concrete work items.
- **Planning:** one role converts discoveries into bounded, non-overlapping specs.
- **Fan-out:** isolated tasks run concurrently and preserve per-item failures.
- **Coder/reviewer:** durable review judgment across bounded revision iterations.
- **Review/merge:** independent coverage gate followed by a compact handoff.

## Hard limits

- Use at most 16 concurrent agents unless the user explicitly requests more.
- Use at most 64 tasks per script unless the user explicitly requests more.
- Do not add generic orchestration timeouts. Use domain deadlines only; Yoke
  cancellation is cooperative and waits for cleanup.
- Never derive paths from unchecked task or run IDs. Use unique filename-safe
  slugs and run-specific artifact directories.

## Completion gate

Do not finish until:

- every selection passed preflight and any required real provider probe;
- every agent result is completed or represented as a terminal error;
- every progress callback error is surfaced;
- every created agent is closed, including partial construction failures;
- raw JSON, logs, review, and final handoff exist under `.agents_local/`;
- write scopes, retries, conflicts, unsupported claims, and validation are
  explicitly accounted for.
