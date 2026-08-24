# SDK thread-based async limitations

## Status

Record-only plan. The current design is intentional and correct for yoke's
workload; nothing here is scheduled. This file exists so the failure modes are
documented with their mechanisms before they are ever hit in production.

## Current design

The SDK runtime is synchronous. `Agent.prompt_async()` bridges into asyncio:

- `src/yoke/ai/sdk/async_support.py:14` (`run_sync_cooperatively`) runs the
  whole prompt via `asyncio.to_thread`, shields the worker, and on timeout or
  cancellation sets a stop event and detaches the worker
  (`observe_worker`) so the original exception reaches the caller immediately.
- `src/yoke/ai/sdk/agent.py:336` (`_async_lock_for_current_loop`) binds each
  agent to the first event loop that prompts it and raises
  `RuntimeError` from any other loop. `fork()` resets the binding
  (`src/yoke/ai/sdk/agent.py:204`).
- `src/yoke/ai/sdk/batch.py:63` bounds `run_many()` with an
  `asyncio.Semaphore(max_concurrency)` (default 8, `src/yoke/ai/sdk/batch.py:41`).
  The semaphore is acquired before per-attempt timeouts start, so batch
  timeouts exclude semaphore waits but include executor queue waits.
- Agent factories also run through `asyncio.to_thread`
  (`src/yoke/ai/sdk/batch.py:254`).

`asyncio.to_thread` uses the loop's default `ThreadPoolExecutor`
(`min(32, cpu_count + 4)` workers). Each worker is pinned for an entire agent
turn, which is minutes, not milliseconds.

## Problem 1: default executor exhaustion

**Mechanism.** Concurrent agent turns share the loop's default executor. On a
typical 8-core machine that is 12 threads. One `run_many(max_concurrency=8)`
can transiently want up to 16 threads: 8 pinned agent workers plus up to 8
short-lived factory workers during staggered starts and retries. Two
concurrent `run_many()` calls, or unrelated `asyncio.to_thread` users on the
same loop (HTTP clients, user code), exceed the pool immediately.

**Symptoms.**

- Tasks report `timed_out` with zero usage records and no provider traffic:
  they queued in the executor while `asyncio.timeout` (which wraps the
  `to_thread` await) kept ticking. Fails on small machines, passes on large
  ones.
- `max_attempts` retries work that never started, doubling latency and cost.
- Batch duration inflates well past `tasks / max_concurrency` expectations with
  no visible provider slowness.

**Fix.** Run agent workers on a dedicated, explicitly sized
`ThreadPoolExecutor` instead of the loop default: replace
`asyncio.to_thread(call)` in `run_sync_cooperatively` with
`loop.run_in_executor(agent_pool(), call)` (a lazily created process-wide pool,
size overridable via an env var such as `YOKE_SDK_AGENT_WORKERS`, defaulting to
the current `min(32, cpu_count + 4)` semantics). Route `_call_factory` through
the same pool so factory and agent occupancy are budgeted together. Document
the interaction between `run_many(max_concurrency=...)` and the pool size in
`src/yoke/docs/sdk.md`.

## Problem 2: zombie workers hold threads after ignored cancellation

**Mechanism.** Cancellation is cooperative: the stop event is latched and the
thread keeps running until the runtime observes it. A tool blocked in a
non-cooperative syscall ignores the fence indefinitely. That worker thread
holds an executor slot, its provider client, and the agent's synchronous
`_prompt_lock` (`RLock`) for the rest of the process lifetime. `close()`
waits a bounded cleanup window, then raises and leaves the agent open for a
retry, but never reclaims the slot.

**Symptoms.**

- A later synchronous `prompt()` on the same agent blocks forever on the
  `RLock` held by the zombie worker.
- Available executor capacity shrinks over a long-lived process; combined with
  Problem 1 this produces eventual queuing with no code change.
- `close()` raises after its cleanup window; `Agent is closing or closed`
  errors on subsequent use of that agent instance.

**Fix.** Partially mitigated by the dedicated pool (Problem 1 fix makes
capacity explicit and configurable). Document that agents whose tools can
block non-cooperatively should be run in a process that can be recycled, and
consider a debug counter of detached workers so capacity loss is observable
instead of silent.

## Problem 3: one-agent-one-event-loop binding

**Mechanism.** `asyncio.Lock` cannot be awaited across loops, so
`_async_lock_for_current_loop` pins the agent to its first loop.

**Symptoms.**

- `RuntimeError: One Agent cannot be used from multiple event loops` in:
  pytest-asyncio (fresh loop per test, which is why `test_sdk_async.py` must
  rebuild agents), scripts that call `asyncio.run()` once per task, and
  notebooks after a loop restart.

**Fix.** Replace the single binding with a per-loop lock registry
(`WeakKeyDictionary` keyed by loop, guarded by the existing threading lock),
allowing sequential migration between loops while still rejecting concurrent
cross-loop use. Defer until this recurs in practice; the workaround (new agent
plus `state_path` restore, which `fork()` already models) is acceptable.

## Problem 4: GIL contention degrades loop responsiveness and timeout precision

**Mechanism.** Worker threads do real CPU work per turn: pydantic validation,
transcript serialization, and per-observer `deepcopy` of every event payload
(`src/yoke/ai/sdk/observability.py:235`). `agent.messages` deep-copies the
full transcript per call and runs on the loop thread inside `run_many`
baseline accounting (`src/yoke/ai/sdk/agent.py:96`). A long `deepcopy` or
`json.dumps` holds the GIL as one uninterruptible stretch, so the loop thread
fires timers late. A shared `JsonlObserver` serializes all agents through its
file lock on the worker hot path.

**Symptoms.**

- Timeouts and cancellation propagate late under heavy batch load.
- The hosting event loop becomes janky for unrelated asyncio work.
- Throughput drops when many agents emit high-frequency events through a
  shared file observer on slow storage.

**Fix.** If measured: budget payload snapshots (bounded copies instead of full
`deepcopy` where redaction permits), move batch baseline accounting off the
loop thread, and give `JsonlObserver` a background queue so its lock leaves
the worker hot path. The redaction/isolation guarantees must not change.

## Problem 5: hooks and observers must be synchronous and thread-safe

**Mechanism.** Event hooks, observers, `before_tool_call`, and
`after_tool_call` run on worker threads by construction. They cannot use
`asyncio`, must not block, and re-entrant agent mutation is rejected via
`_prompt_owner` (`src/yoke/ai/sdk/agent.py:359`).

**Symptoms.**

- A hook that awaits anything is broken by design; a blocking hook stalls only
  its agent, which looks like provider latency.

**Fix.** Documentation only: keep the thread-safety requirements prominent in
`src/yoke/docs/sdk.md`. If async hooks are ever demanded, they must be
marshalled back to a loop via thread-safe primitives, never awaited inline on
the worker.

## Why this is acceptable today

- The workload is I/O-shaped: provider calls wait on sockets and tools wait on
  subprocesses, both of which release the GIL.
- Per-agent serialization plus the batch semaphore keep steady-state
  concurrency low; the islandlord-style fan-out (7 domains) never approaches
  the limits above.
- One synchronous runtime keeps cancellation, closing, and durable-state
  semantics coherent instead of maintaining two half-implementations.

## Non-goals

- No streaming surface: agents consume completed turns; observers already emit
  events at tool-call boundaries, which is the actionable granularity.
- No rewrite to natively async providers.
- No removal of the synchronous runtime or `prompt()`.

## Acceptance tests (when implemented)

- Pool exhaustion: with a one-worker dedicated pool, two concurrent
  `prompt_async()` calls must show documented behavior (second call queues,
  its timeout budget includes the queue wait).
- Pool isolation: saturating the default executor with unrelated `to_thread`
  sleepers must not delay agent workers on the dedicated pool.
- Zombie accounting: a tool that ignores cancellation must raise from
  `close()` after the bounded window, keep the agent open, and increment the
  detached-worker counter.
- Loop migration (Phase 3 of Problem 3): sequential use from a second loop
  succeeds; concurrent cross-loop use still raises.
- Existing async suite (`tests/yoke/ai/test_sdk_async.py`) passes unchanged
  except where new semantics are intentionally documented.
