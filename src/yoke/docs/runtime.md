# Runtime implementation

`RuntimeAgent` owns provider configuration, conversation state, tool registration
and tool lifetimes. Its `run()` creates an `AgentRun` with a working context,
callbacks and stop signal. `ModelRequests` owns that run's provider calls,
compaction handoffs and overflow recovery. Manual compaction uses the same
request implementation.

Context initialization lives in `ContextManager`. Stateless transcript mutations
and views live in `context/history.py`; tree reconstruction lives in
`context/helpers.py`. Prompt assembly is the concrete `build_prompt_context()`
function. `Compactor` remains the concrete estimator with model and provider
settings used by context budgeting. `ContextManager` accepts instructions,
compaction policy and an estimator. The unused `prompt_builder`,
`transform_messages` and `convert_messages` constructor hooks have been removed.

Callers can use `tool_definitions()`, `tool_root`, `context_for_messages()` and
`replace_context()` instead of reading runtime fields. Context values are mutable;
copy a returned context before making speculative changes. `promote()` accepts a
completed runtime fork. Tool discovery values shared with CLI loaders live in
`agent/tools/registration.py`.

Tool resources have runtime-owned leases. A weak directory coordinates owners
that share a resource, including separately constructed runtimes. It retains
neither owners nor resources. The final release closes resources outside the
lease lock and attempts every close before raising the first error.
Each installed tool index carries its own in-process worker owner. Runtime
close cancels and joins that index's workers before releasing resources. This
preserves the previous grouping when registration replaces the index. A shutdown timeout keeps
the runtime open so its owner can retry cleanup.

Managed commands live in `agent/tools/command_process/`. Its public imports are
`CommandProcessManager`, `CommandProcessResult`, `CommandProcessOutputPage`,
`CommandProcessSnapshot` and `command_completion_event_id`. Child processes,
admission, stdin, retained output and process-tree cleanup are package internals.

The runtime calls `complete_with_cancel()` when a provider implements it and
otherwise calls `complete()`. The unused `complete_with_context()` dispatch
protocol and request metadata plumbing have been removed. Built-in providers
continue to own their cache keys and response chains. Codex derives its prompt
cache key from provider configuration, independently of the removed metadata.
Image projection still replaces images for text-only models and preserves them
for image-capable models. The existing per-message image guard remains; there is
no request-wide image truncation setting.

Public package exports remain lazy. Each package has one `_LAZY_EXPORTS` table;
`__all__` comes from its keys. Generate the matching typing stubs with
`uv run python docs/tools/generate_exports.py`, then format them with Ruff.
`--check` and `test_lazy_export_tables.py` detect drift.

`tests/yoke/loop/test_runtime_request_differential.py` freezes 16 provider calls
from revision `5268d70`. Its fixture records complete messages JSON, tool schemas,
call kinds and the configured Codex cache key across tools, parallel calls,
skills, image capabilities, compaction, overflow, stop/continue and tree navigation.
Keep the fixture unchanged during structural refactoring. The provider request
contract and HTTP/compaction projection guards cover the lower layers too.

Agent-run reporting closes its listening socket with `shutdown()` before
`close()` so an idle `accept()` in the listener thread wakes immediately.
Closing the registry no longer waits for the accept timeout.

`storage.database.Database(path, *, synchronous="NORMAL")` keeps the normal
SQLite durability policy by default. `synchronous="OFF"` is available for
explicitly disposable databases. The non-storage test fixture uses that setting
for its isolated schema copies; storage tests and production callers retain
`NORMAL`.
