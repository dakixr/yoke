# SDK reference

Import from `yoke.ai`, `yoke.ai.types`, `yoke.ai.skills`, `yoke.ai.providers`,
and `yoke.ai.utils`. Keep `yoke.ai.sdk.*` implementation imports out of orchestrators.
The examples in [PATTERNS.md](PATTERNS.md) contain complete imports; this reference
covers caveats rather than prescribing setup for every task.

## Provider selection

Yoke resolves `provider`, `provider:model`, or `provider:model:thinking_effort`.
The larger CLI examples require an explicit model. Built-ins are `codex`, `opencode-go`,
and `zai`; installed global plugins under `~/.yoke/providers` also participate.

`print_builtin_provider_status()` takes no arguments and prints local readiness,
models, thinking efforts, and selection strings. Use it to gather context before
authoring the orchestrator. `builtin_provider_status()` returns the same kind of
metadata for programmatic checks. Both are public imports from `yoke.ai.utils`.

`build_builtin_provider(selection, session_id=...)` constructs a provider using
environment and local credential configuration. An unknown catalog model raises,
but unsupported thinking effort falls back to the model default. Compare exact
requested values with metadata when fallback would violate the task. The larger
examples' `preflight()` rejects mismatches, constructs a provider, and closes its owning
agent. It deliberately requires advertised catalog metadata; adapt that check for
a custom provider without a model catalog rather than guessing its supported values.

These are local constructability checks, not remote health checks. When changing
a provider implementation, separately test a real function-tool/result round trip.
Ordinary orchestration does not require modifying or re-probing provider internals.

`available_builtin_providers(selections=...)` constructs instances and skips
selections raising `ValueError`. It is not strict validation or a factory for a
batch. Every returned instance needs an owner and cleanup; prefer fresh construction
inside the worker factory rather than distributing shared providers.

## Configuration and capabilities

`Agent(provider=...)` without `config` grants the default coding tools, including
write, shell, network, and supported image capabilities. An explicit
`RunConfig(root=..., tools=...)` avoids that implicit tool grant.

| Capability | Behavior |
| --- | --- |
| `file.read` | Text read plus best-effort document/image extraction tools. |
| `file.search` | Native workspace search tools with portable fallbacks. |
| `file.write` | Model-aware patching or edit/write tools. |
| `shell` | Shell, process interaction, and Python execution. Not read-only. |
| `web.fetch`, `web.search`, `web.research` | Network research. Codex research prefers hosted search and can fall back to the local workflow; other providers use local search/fetch/synthesis. |
| `image.attach` | No tool when provider/model metadata rejects image input. |
| `image.generate` | Codex-hosted generation, omitted for unsupported providers. |
| `mcp` | Configured MCP discovery/calls; close the agent to release resources. |

Capability IDs resolve through the same provider-aware registry as the CLI.
`RunConfig.root` is a path base, not an access-control boundary. `skills` defaults
to empty; configure relevant skills with `Skill.from_dir(...)` or `Skill.inline(...)`
from `yoke.ai.skills`. `include_agents_file` defaults to true, so repository
instructions can load, but the parent's conversation and active skills do not transfer.

## Process tools for launching workers

Native Yoke and MCP use `command_exec`, `python_exec`, `process_input`,
`process_read`, and `process_cancel`. Both execution tools default to
`mode="auto"`; the host owns the initial completion window, 30,000 ms natively
or the configured MCP default. `mode="background"` returns immediately. Choose
longer waits on `process_read(wait_ms=...)` after a handle exists. MCP read waits
use the configured remote cap, at most 240,000 ms.

`process_read` accepts 1 to 16 unique session entries containing `session_id`
and an optional opaque cursor. Its default
`until="completion"` waits for all requested sessions, not for ordinary output
or a full output budget. Use `until="output_or_completion"` to return on any
unread output or terminal session, or `wait_ms=0` for a snapshot. Reads default
to 60,000 ms; native reads allow up to 3,600,000 ms, one hour. One deadline
applies to the whole batch. Expiry leaves workers running. Python's `timeout`
is a separate execution deadline in seconds, not a read wait or an SDK prompt
timeout.

Copy each item's returned `cursor` unchanged into the matching session entry on
the next read. Do not decode or construct cursor values. Reads do not consume output. Inspect
`exit_code` even when the read's `ok` is true, and continue paging after exit
while `has_more_output` is true. Account for `gap` when history has been
discarded. The total read `max_bytes` defaults to 32,000
and accepts 1,024 through 64,000, across all requested sessions.

`process_input` requires nonempty `chars`; use `process_read` for polling.
Its optional cursor identifies already-read output and must match the target
session. Without one, output starts at the earliest retained record. Input
waits default to 250 ms and accept 0 through 5,000 ms. `process_cancel`
terminates the owned process tree while preserving final output for paging;
MCP also revokes Python's bridge token and cancels its managed child operations.
Cancellation does not create a new output cursor, so keep the last cursor from
execution, reading, or input when final output matters.

Update callers using `exec_command`, `exec_python`, `write_stdin`, or
`process_io`; these are not callable aliases. Remove execution-level
`yield-time_ms` or `wait_ms`, choose semantic `mode` at launch, and use
`process_read.wait_ms` for explicit follow-up waits. Replace empty-input polls
with cursor-based `process_read` calls. See the
[shared process reference](../../../../docs/process-tools.md) for result reasons,
cursor errors, retention, and the full migration table.

## Durable state

`Agent(state_path=...)` automatically loads that file if it exists. With
`autosave=True`, successful `prompt()` and `prompt_async()` calls save snapshots.
Failed or interrupted turns are not guaranteed to be saved. Autosave requires
a bound state path; it does not checkpoint filesystem edits or orchestrator control flow.

Use `.agents_local/<run-id>/<task-id>.<role>.json` after validating IDs as unique
filename-safe slugs. A fresh unrelated job needs a fresh run ID. Resume only the
same task with compatible instructions, tools, and provider. Keep one writer per
state file. For a single role, an explicitly chosen unique state path is enough;
see the small durable example in [PATTERNS.md](PATTERNS.md). The larger review-pair
example checks a persisted contract before resuming.

For OpenCode Go, pass a stable run/task/role `session_id` whenever rebuilding that
role's provider. It becomes `x-opencode-session`; omitting it creates a fresh
session identity. A session header alone does not load conversation state.

`agent.save(path=None)` writes state and binds the destination.
`Agent.load(path, provider=..., config=...)` creates an agent with fresh runtime
dependencies. `agent.restore(path)` replaces state and rebinds its path while
retaining the current provider and configuration. Snapshots do not serialize
credentials, provider objects, tool instances, or callbacks. Treat their contents
as sensitive conversation data.

## Async calls, failures, and retries

`prompt_async()` mirrors `prompt()` and adds an optional timeout. Calls on one
stateful agent serialize, and that agent binds to its first async event loop.
Use independent agents with `run_many()` for concurrency.

`run_many()` accepts ordered `BatchTask` values and a sync or async factory.
It runs synchronous factories off the event loop, requires a fresh agent and
provider on every attempt, closes owned agents, preserves input order, isolates
item errors, and aggregates available provider-reported usage. Item statuses are
`completed`, `error`, and `timed_out`. They describe execution, not whether the
task's acceptance criteria passed. Inspect returned structured blockers too.

Retries create fresh workers, not filesystem rollbacks. A throwing retry policy
becomes an item error. Progress-callback exceptions go into `batch.progress_errors`.
With `output_type=...`, structured output parsing can run up to three attempts
within one prompt, regardless of batch `max_attempts`. Those correction turns
can still call tools. Exhaustion raises `StructuredOutputError`.

Cancellation and timeouts signal the synchronous worker cooperatively. The
async prompt caller receives cancellation/timeout while that worker may still
run until it observes the signal. `close()` waits for active work; `aclose()`
does so without blocking the event loop. Batch cancellation waits for cleanup.
Prompt timeout includes that agent's queue wait, not the whole batch's semaphore
queue, factory work, or cleanup. Blocking dependencies need their own timeouts.
`RunConfig` has no iteration cap; use `stop_requested` for explicit cancellation.

## Observation

Observer detail levels are `quiet`, `messages`, `actions`, and `full`. Batch
observation adds task IDs and retry-attempt numbers. Use a batch observer rather
than installing the same observer on every factory-created agent. Direct roles
can use labeled agent observers, as in the pair example.

Console rendering is for live visibility, not durable or lossless result
delivery. `ConsoleObserver` truncates rendered events to its configured
`max_length`, 1000 characters by default. Consume `AgentResult.output` and batch
item results directly, or persist them, when the complete answer matters.

Built-in renderers redact credential-like argument keys, not arbitrary secrets
inside strings. Full traces remain sensitive. Observer failures are logged and
do not fail the task; they are distinct from `progress_errors`. Check that the
expected trace exists and is readable before claiming it was retained.
