# Shared process tools

Native Yoke and the tool-only MCP server use the same process tool names and
cursor contract. Process handles belong to the live runtime, not to persisted
Yoke conversations. Restarting that runtime loses the handles and terminates
its live processes.

| Tool | Purpose |
| --- | --- |
| `command_exec` | Launch shell text with `cmd` or a direct process with `argv`. |
| `python_exec` | Run Python in a managed subprocess. |
| `process_input` | Write nonempty input and collect a short response. |
| `process_read` | Wait for or page output from one or more processes. |
| `process_cancel` | Terminate an owned process tree while retaining final output. |

## Launch and wait budgets

Both execution tools accept `mode="auto"` or `mode="background"`, with `auto`
as the default. Auto uses a host-owned initial completion window, 30,000 ms
natively and the MCP server's configured default remotely. Background returns
immediately. A process that finishes within the initial wait returns immediately;
otherwise it keeps running under its returned `session_id`.

| Operation | Default wait | Native maximum | MCP maximum |
| --- | --- | --- | --- |
| `command_exec`, `python_exec` in auto mode | 30,000 ms native; configured default on MCP | Host-owned | Host-owned, capped by server configuration |
| `process_read` | 60,000 ms | 3,600,000 ms, one hour | Configured `max_remote_wait_ms`, at most 240,000 ms |
| `process_input` | 250 ms | 5,000 ms | 5,000 ms |

`process_read` and `process_input` waits accept zero; negative waits are invalid.
MCP's remote read cap may be configured lower than 240 seconds; consult the
active tool schema and server configuration. The existing MCP
`--default-yield-ms` setting controls the host-owned auto execution window. It
is server configuration, not a model-facing execution argument.

`python_exec.timeout` is a separate execution deadline, measured in seconds.
It can terminate Python even while a read is waiting. Expiry of an execution
tool's initial wait or a `process_read.wait_ms` budget never kills the process.
A read returning `reason="deadline"` is not a Python timeout; inspect
`timed_out`, `running`, and `exit_code` for the process outcome.

Launch in the background with `command_exec`:

```json
{"argv":["uv","run","python","job.py"],"mode":"background"}
```

Or use auto mode and let the host own the initial wait:

```json
{"code":"print('ready', flush=True)","mode":"auto","timeout":180}
```

If the process is still running, choose a longer follow-up wait with
`process_read(wait_ms=...)` rather than tuning launch timing.

## Read until completion, output, or a snapshot

`process_read` takes these fields:

| Field | Contract |
| --- | --- |
| `sessions` | Required list of 1 to 16 `{session_id, cursor?}` entries with unique session IDs. |
| `until` | `"completion"` by default, or `"output_or_completion"`. |
| `wait_ms` | 60,000 by default, bounded by the host maximum above. |
| `max_bytes` | 32,000 by default; 1,024 to 64,000 total across the batch, not per session. |

Completion mode waits for **all** requested sessions to become terminal or for
the single batch deadline to expire. Ordinary output and a full response
budget do not end this wait. Output-oriented mode returns on **any** unread
output or terminal session. It is useful when an interactive program needs
input before it can finish. Errors, including unknown sessions and invalid
cursors, return promptly rather than waiting for the deadline. Items and
errors preserve request order.

Use the actual `cursor` from each preceding result in a read:

```json
{
  "sessions": [
    {"session_id":12345,"cursor":"pc1_AAAAAAAAMDkAAAAAAAAAAwAAAAAAAAAA"},
    {"session_id":23456,"cursor":"pc1_AAAAAAAAWqAAAAAAAAAAAQAAAAAAAAAM"}
  ],
  "until":"completion",
  "wait_ms":60000,
  "max_bytes":32000
}
```

These cursor values are illustrative. Cursors are opaque continuation tokens.
Copy them unchanged rather than decoding, editing, or constructing them. Omit a
cursor to read from the earliest retained output for that session. To inspect available output without
waiting, set `wait_ms=0`. This is a snapshot in either mode, even when the
process is already finished. After a read deadline, continue with the returned
cursors rather than launching the command again.

The read envelope is `{ok, reason, items}`. Normal reasons are:

- `completed`: all requested sessions are terminal in completion mode; any
  terminal session can trigger this in output-oriented mode.
- `output`: unread output triggered an output-oriented read.
- `deadline`: the read's wait budget expired before its condition was met.
- `snapshot`: a zero-wait observation, except when an explicit error applies.

`error` and `cancelled` are exceptional reasons. This interface uses explicit
reads; it does not promise runtime push delivery or output batching thresholds.

## Automatic completion notices

The native agent adds a status-only notice before its next model call when a
background command finishes. Notices batch completed sessions and include their
session IDs, statuses, exit codes, elapsed times, commands, and working directories.
They do not include command output or advance output cursors. Output remains in
the process tools, so a notice cannot replay logs from an earlier read.

Use `process_read` with the last returned cursor for each session when its output
is needed. A read that reports the session as terminal suppresses a later notice
for that completion. A read made while the session is still running does not
suppress its eventual completion notice. Notices also report
`older_events_dropped` when the bounded completion queue evicts older events.

These notices are native agent context messages, not an MCP push-delivery
contract. MCP clients continue to use explicit process reads.

## Input and cancellation

`process_input` requires `session_id` and nonempty `chars`. It also accepts
`wait_ms`, defaulting to 250 and bounded to 0 through 5,000, optional
`max_output_tokens`, and optional `cursor`.

The cursor describes output already read, not where to write stdin. When
provided, it must have been returned for the target session. Yoke validates that
before any input is written.
Without a cursor, the response starts at the earliest retained output, so it
may repeat text from an earlier read. It never skips unread output silently.
After writing, the call collects output for the short wait window or until
completion. A call cancelled before the write does not write anything.

`input_written` is true once the write succeeds, false when it was not attempted,
or null when an OS error leaves delivery uncertain. A failed response read does
not undo input. Never resend input just because its response read failed; inspect
the process with `process_read` first.

Writes retry partial OS writes and observe cancellation while queued or blocked
by stdin backpressure. Delivery has a separate five-second safety limit before
the short response window. On interrupted delivery, `input_bytes_written` gives
the known byte count accepted by the OS; input is not resent automatically.

```json
{
  "session_id":12345,
  "chars":"yes\n",
  "cursor":"pc1_AAAAAAAAMDkAAAAAAAAAAwAAAAAAAAAA",
  "wait_ms":250
}
```

Use `process_read` to poll. Empty `chars` is invalid, not a polling shortcut.
For a program awaiting further input, use `until="output_or_completion"` or
a zero-wait snapshot instead of waiting for completion.

`process_cancel` takes `session_id` and terminates that owned process tree.
It is safe to call on a retained finished session and preserves pageable final
output. On MCP it also revokes the Python bridge capability token and cancels
its managed child operations. Cancellation is a lifecycle operation, so its
result does not create or reset an output cursor. Read retained final output
with the last cursor you already had. Cancellation does not erase cursor history.

At runtime process capacity, a new launch fails rather than terminating existing
work. Finish or explicitly cancel a process to make room. Retired session IDs are
not reused within the same runtime. Cancelling queued work prevents it from
starting once admission becomes available.

## Results, paging, and retention gaps

Execution's `ok` describes the process outcome. For input and reads, `ok`
describes the operation, so a successful read can contain a nonzero exit code.
Always inspect `exit_code`, which is present even when its value is null for
a running process.

Successful process items retain `session_id` after exit and include `running`,
`exit_code`, `timed_out`, `output`, `elapsed_seconds`, `cursor`,
`has_more_output`, and `gap`. Paging preserves
decoded terminal text, including carriage returns and final newlines. Legacy
inspector tails and low-level consuming reads retain their normalized display;
the new process tools do not normalize line endings. Initial execution and input return
their single process item plus `reason`. `next_tool` can be `process_read`
while the process is running or has more output; there is no
`recommended_wait_ms` field.

A cursor is a short versioned opaque string such as `pc1_...`. Internally Yoke
tracks the retained-output record and byte position, but those details are not
part of the public interface. Reads are non-consuming and repeatable while the
referenced history is retained. Advance with the returned `cursor` to collect
the next page. Reusing an old cursor can repeat output; it does not acknowledge
or consume a stream. A malformed cursor or one from another session is an error.

`has_more_output` is independent of `running`. Even after exit, timeout, or
cancellation, keep reading with the returned cursor until all needed output
has been collected. A completed result may still need several snapshot pages.
Initial output limits, input responses, and completion reads all preserve the
cursor needed to retrieve undisplayed retained output.

Retention is bounded. `gap` reports discarded history. The cursor advances
across lost history only with that explicit gap report. Report the loss when it
affects the answer; a gap is not proof that the missing output was empty.
Completed histories remain pageable until eviction. After a session itself
has been evicted, its handle returns an error rather than restarting work.

New model-facing results preserve cursors, gaps, `has_more_output`, and exit
codes. Legacy saved results retain their established provider projection;
resuming a conversation does not rewrite old tool results into this contract.
MCP process results include the complete page as JSON text as well as structured
content, even when `legacy_result_text=False`, so clients retain cursor visibility.

## Breaking migration

Update tool allowlists, callers, and generated requests on both native Yoke and
MCP. The old model-facing names are neither advertised nor callable aliases.

| Old interface | Replacement |
| --- | --- |
| `exec_command` | `command_exec` |
| `exec_python` | `python_exec` |
| `write_stdin` or `process_io` with input | `process_input` with nonempty `chars` |
| Empty-input polling through `write_stdin` or `process_io` | `process_read` with a `sessions` cursor list |
| Execution `yield-time_ms` or `wait_ms` | Use semantic `mode`; choose longer waits on `process_read(wait_ms=...)` after launch |
| `next_cursor` from the old MCP reader | `cursor` on each process item |
| Structured process cursor fields such as `after_seq` and `offset` | Opaque `cursor` string copied unchanged with its `session_id` |
| `recommended_wait_ms` | Choose an explicit `wait_ms` within the host limit, or use its default |

Existing Python imports such as `ExecCommandTool`, `CommandTool`, and
`PythonExecTool` remain usable; `CommandExecTool` is the command class alias.
Import names do not restore the removed public tool names. See the
[SDK tool reference](sdk.md#built-in-tools) for the process tool classes.
