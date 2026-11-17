# Runtime internals

This page is for contributors who change the agent loop, providers, tool
execution or session storage. User documentation is in [CLI](cli.md),
[Sessions](sessions.md), [Configuration](configuration.md) and [SDK](sdk.md).

Read the provider stability rule in the repository's `AGENTS.md` before you
touch anything on the request path. Provider message content and order, system
and skill instructions, tool call sequencing, image projection, compaction
handoffs, response continuity and cache scope must not change unless the task
asks for it.

## Process layout

The CLI, the web UI and ACP are clients of one per-user daemon, in
`yoke.daemon`. The daemon hosts the HTTP runtime in `yoke.http`, which owns
sessions, turns, queues, compaction and workspace relocation. Terminal clients
only render events and send input. The SDK runs `RuntimeAgent` in the caller's
process with no daemon.

Before serving, the daemon imports every package a turn can reach, such as
`yoke.agent`, `yoke.ai`, `yoke.http` and `yoke.storage`. A checkout updated under a running daemon therefore
cannot mix new modules into the old process. The terminal UI packages
`yoke.cli.interactive` and `yoke.cli.render` are never imported by a turn.

Each client attachment carries its environment and working directory. The
daemon binds them with `yoke.environment.execution_environment()` while it
builds providers and runs tools. Code that needs an environment variable for a
session should call `environment_values()`, not read `os.environ`. A daemon
spawned by a client starts with the short allowlist in
`yoke/daemon/environment.py`.

The route-level contract between terminal clients and the daemon is in
[CLI session backend](cli-backend.md) and [Daemon client contract](daemon-client.md).

## The agent loop

`RuntimeAgent` (`agent/loop/agent.py`) owns provider configuration,
conversation state, tool registration and tool lifetimes. `run()` creates an
`AgentRun` (`agent/loop/iteration.py`) with a working context, callbacks and a
stop signal. `ModelRequests` (`agent/loop/requests.py`) owns that run's provider
calls, compaction handoffs and overflow recovery. Manual compaction uses the
same request code.

`ContextManager` (`agent/context/manager.py`) takes instructions, a compaction
policy and an estimator. Stateless transcript changes and views are in
`agent/context/history.py`, and tree reconstruction is in
`agent/context/helpers.py`. `build_prompt_context()` in `agent/prompting.py`
assembles the prompt. `Compactor` (`agent/compaction/core.py`) is the token
estimator used for context budgeting.

Callers use `tool_definitions()`, `tool_root`, `context_for_messages()` and
`replace_context()` instead of reading runtime fields. Contexts are mutable, so
copy a returned context before changing it speculatively. `promote()` accepts a
completed runtime fork. Tool discovery values shared with the CLI loaders are in
`agent/tools/registration.py`.

The runtime calls a provider's `complete_with_cancel()` when it has one and
`complete()` otherwise. Built-in providers own their cache keys and response
chains.

## Provider requests and caching

Codex keeps one Responses WebSocket per provider and holds response continuity,
encrypted replay state and routing metadata in memory. None of that is saved
with a session or an SDK state file. A resumed session rebuilds context from
the visible conversation.

The session ID is the Codex cache scope. Every Codex request and WebSocket
handshake sends it as the `session_id` header, so a reconnect after an idle
close, and any load balancer in front of Codex, stays on the warm prompt cache.
New and forked sessions get new scopes. Leading system messages become the
Codex `instructions`. System context added later, such as a skill activated mid
session, is sent in place as a `developer` input message, so the cached prefix
and response continuity survive.

OpenCode Go sends one stable `x-opencode-session` value per Yoke session,
including retries and resumes. Forks get a new value. OpenCode Go chat requests
stream internally and return one complete response. A truncated stream fails
without running partial tool calls.

Image projection replaces images with text for models that do not accept them
and keeps them for models that do. There is a per-message image guard and no
request-wide image limit.

## Compaction

Compaction appends a synthetic handoff instruction to the current provider
context and stores the assistant's answer as a checkpoint. It then starts a
smaller context with the stable instructions, earlier real user prompts, the
newest handoff and the messages after the checkpoint. Active skill instructions
stay before the retained prompts. Retention selects `user` entries from the
active branch, so command completion notices, branch summaries and tool context
are summarized but do not become retained user prompts. SDK structured-output
correction prompts also use control entries; schema instructions attached to
the original user prompt stay with that prompt. Repeated compaction
keeps the checkpoint's retained values, including truncated prompts. The
canonical session is append only and keeps everything.

For a 400,000-token window, pre-handoff user prompts are capped at 20,000 tokens,
newest first, and the handoff prompt asks for at most 12,000 tokens. Smaller
windows scale both limits (`agent/budget.py`). One boundary message may be cut
at the end to keep the cap exact. The handoff is a normal continuation request,
so the current context must still fit the provider. Automatic threshold
compaction and overflow recovery use the same flow. Overflow retries at most
once after a successful reduction.

A model switch to a smaller window may compact first. If the handoff fails or
the smaller context still does not fit, the switch rolls back. A failed
cross-provider switch closes the new provider and restores the previous one.

Provider-reported token counts are used for budgeting only when their provider
and model match the active model. When saved usage understates a rebuilt or
resumed prompt, budgeting falls back to the conservative estimate.

## Tool execution

Most tool calls run in child processes started with the `spawn` method, which
avoids `fork` in a multithreaded process. After a cancel, a background reaper
continues termination and kill escalation. Tools that cannot be spawned, or
that set `execute_in_process`, run in supervised daemon threads. Cooperative
tools watch the cancellation callback. Non-cooperative tools are detached and
keep their resources until they return.

Stopping or steering a turn retires it at once and targets a UI handoff under
100 ms. The replacement turn does not wait for the old provider request or tool
to exit. Retired generations are fenced, so late output cannot replace the
accepted conversation or render into the active turn. The 100 ms target is a
logical handoff. It does not mean every remote request, thread or process has
exited.

Interruption markers are stored as `control` entries. The provider receives
one system note, "The previous response was interrupted by the user.", before
the next user message. Further interruptions without assistant output share
that note. Resuming without a new user message places the note at the end of
the context. Exact legacy assistant notices receive the same projection on
load without changing stored entries. Terminal scrollback shows `Interrupted`.

Runtime shutdown signals the remaining in-process tools and waits for a bounded
cleanup window. If a tool outlives it, `close()` raises and leaves the runtime
open so the owner can retry.

Tool resources have runtime-owned leases. A weak directory coordinates owners
that share a resource, including separate runtimes, without keeping owners or
resources alive. The final release closes resources outside the lease lock and
attempts every close before raising the first error. Each installed tool index
has its own worker owner. Runtime close cancels and joins that index's workers
before releasing resources.

Managed commands live in `agent/tools/command_process/`. Its public imports are
`CommandProcessManager`, `CommandProcessResult`, `CommandProcessOutputPage`,
`CommandProcessSnapshot` and `command_completion_event_id`. Child processes,
admission, stdin, retained output and process-tree cleanup are internal.

Agent-run reporting calls `shutdown()` on its listening socket before `close()`,
so an idle `accept()` in the listener thread wakes at once.

## Tool result projection

Tools return full JSON results, and the session stores them whole. Before a
provider request, `agent/tool_result_projection.py` shrinks selected built-in
results. `rg`, `fd`, `apply_patch` and `web_search` use TOON 4.1 tables where
that removes repetition. For `rg`, a match with one submatch is flattened into
text and offset columns, and rows with several submatches stay JSON. Process
tools keep JSON but drop duplicate bookkeeping. They keep output, session IDs,
cursors, gaps, `has_more_output`, exit codes, errors, cancellation and timeout
state.

Projection provenance is stored on the tool result entry. Resuming a session or
changing the tool registry cannot reinterpret old results. Results replaced by a
hook, and custom subclasses, do not inherit a built-in projection. Old entries
without provenance stay canonical JSON. Custom message transforms run before
projection and see the full result. The CLI tool inspector, handoffs and audit
views use the stored JSON.

## Session tree

`SessionTree` (`agent/session_tree/`) is the only code that edits session
topology. It owns parent assignment, active selection, legacy repair, branch
reconciliation and compaction checkpoints, and produces runtime, provider, audit
and scrollback projections. Callers append intents and ask for projections.
They do not edit entry IDs or rebuild paths.

An active session validates the whole topology once and keeps entry and parent
indexes. Prompt, stop, steer and context estimate paths walk and copy only the
active branch. An isolated turn takes ownership of its copied active path and
hands the context back when it is accepted. Normal saves prove that the runtime
branch extends the stored active path and insert only the new suffix. Branch
changes and legacy inputs use full reconciliation.

The provider sees the current compaction epoch. `/tree` sees the full active
path. Detached checkpoints stay audit state and never enter another branch's
prompt. Startup scrollback shows the latest 400 user-visible audit messages.

## Storage

`yoke.storage` holds the SQLite layer and `yoke.session.repository` the session
repository on top of it. CLI startup, HTTP, ACP and handoff code use the same
repository. HTTP lists and pages select metadata or entry IDs in SQL and decode
only the payloads they return.

Every write is one `BEGIN IMMEDIATE` transaction with a 5-second busy timeout.
Queue and tree edits compare and swap on stored revisions, so a conflicting edit
fails instead of overwriting. Edits to `config.json` use the portable file lock in
`yoke._file_io`. Workspace leases live in daemon memory.

Each entry row points at a SHA-256 content blob that forks share. Label and
metadata edits are stored beside the entry. Binary images are stored once by
hash, including copies inside handoffs and metadata overrides, and loading
rebuilds the exact saved data URLs. Raw provider usage payloads are not stored.
Only normalized token counts are.

A failed turn write rolls back before repository records, append cursors or tree
indexes advance. A turn agent is promoted only after its write succeeds. A failed
fork is retired, and the runtime state still matches the saved branch.

`Database(path, *, migrate=False, synchronous="NORMAL")` and
`open_database(path, *, migrate=False)` in `storage/database.py` attach to an
existing database at the exact schema version. `SessionStore` also defaults to
`migrate=False`. Only the daemon's startup store and `yoke db migrate` pass
`migrate=True`, and `yoke db migrate` holds the daemon lifetime lock while it
runs, so it refuses while a daemon is alive. Queue, admission, event and SDK
usage helpers, the CLI and the read-only `yoke db` commands only attach. `synchronous="OFF"` is for disposable databases.
The non-storage test fixture uses it for its schema copies.

A schema upgrade holds `BEGIN IMMEDIATE` while a second connection copies the
committed database with SQLite's backup API to `yoke.db.pre-v<N>`. The backup
must succeed before any migration SQL runs. Schema v4 added structures for
incremental image compaction. Schema v5 added persistent compaction failure
counts.

Idle image compaction converts at most two old blobs per pass. It prepares
images outside the write transaction and commits each replacement with its
references and cache revisions. It skips sessions with active admissions or
compactions and any session used in the last 10 minutes, including other
sessions that share a blob. A disposable worker process decodes images with a
5-second timeout and a 20-million-pixel limit. Truncated images, decoder errors,
crashes and timeouts leave the image inline and record a failure marker so a
restart does not retry it.

## Startup cost

`yoke --help` and `yoke version` import no provider, tool, skill or interactive
modules. OpenAI-compatible providers create their HTTP transport on the first
request. MCP config is read during tool registration so the model sees
`mcp_inspect` and `mcp_call`, but the MCP client modules load only when an MCP
tool runs. Rich Markdown loads on first render.

## Public exports

Public packages export names lazily. Each package has one `_LAZY_EXPORTS` table,
and `__all__` comes from its keys. After changing a table, regenerate the typing
stubs and format them:

```bash
uv run python docs/tools/generate_exports.py
uv run ruff format .
```

`generate_exports.py --check` and `test_lazy_export_tables.py` detect drift.

## HTTP runtime phases

`http/runtime/phase.py` defines the immutable `Idle`, `TurnActive`, `Retiring`
and `OperationActive` values and their transition table. `RuntimeStatus.enter`
validates every phase change, logs its cause at DEBUG, increases the runtime
revision and publishes `session.active.changed`. Snapshots use the same
projection. `Idle` carries the last error and a local reason when queued work
must remain stopped. Queue holds become durable in the input lifecycle phase.

| Current phase | Allowed next phases |
| --- | --- |
| Idle | Idle for error updates, TurnActive, OperationActive |
| TurnActive | TurnActive for activity changes, Retiring, Idle |
| Retiring | Idle, TurnActive for steering |
| OperationActive | Idle, TurnActive for queued work |

`TurnActive` activity changes retain the execution identity. Permission and
question waits post status callbacks to the loop and report `waiting_input`.
Model selection and manual compaction both report `running`. Stop fences a turn
before saving its interruption. Guarded retirement clears logical ownership and
publishes an error even when persistence fails. Manual compaction acquires
logical ownership only after its start marker is durable. Operation cancellation
clears ownership, including cancellation while waiting for capacity. A running
operation worker keeps its lane and capacity until physical cleanup completes.

`TurnExecution` owns `ExecPhase` milestones, from `RESERVED` through
`WAITING_SLOT`, `SLOT_HELD`, `WORKER_RUNNING` and `EXECUTING` to `DONE`.
Its `release_slot` method releases acquired capacity once. Terminal executions
retain their final milestone so cleanup can distinguish submitted workers from
work that never reached the executor. Worker stop and retired events remain
thread-safe fences. Logical retirement and physical worker cleanup remain
separate lifetimes.

Set `YOKE_STATE_CHECKS=1` to raise on ownership invariant failures. Tests set it
automatically. Runtime transitions log the machine, session, previous and next
phase, cause and revision. These changes affect daemon status and ownership;
provider requests, instructions and cache identity retain their existing form.

## Tests

```bash
uv run pytest                          # full suite, in parallel workers
uv run pytest -n 0 path/to/test.py     # one file, serially, for debugging
```

Test fixtures keep `HOME`, runtime directories and databases apart from your
own state. Storage tests keep real migration and crash recovery checks.

`tests/yoke/loop/test_runtime_request_differential.py` freezes 16 provider calls
recorded at revision `5268d70`. The fixture
`tests/yoke/loop/fixtures/runtime_requests_5268d70.json` records full message
JSON, tool schemas, call kinds and the Codex cache key across tools, parallel
calls, skills, images, compaction, overflow, stop and continue, and tree
navigation. Keep it unchanged during refactoring. The provider request contract
tests and the HTTP and compaction projection tests guard the lower layers.
