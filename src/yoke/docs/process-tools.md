# Shared process tools

This is the reference for Yoke's process tools, for anyone writing prompts,
skills or clients that start and watch commands. Native Yoke agents and the
tool-only MCP server ([mcp-server.md](mcp-server.md)) use the same tool names,
fields and cursor rules. Differences between the two are called out where they
apply.

Process handles belong to the running agent runtime or MCP server, not to the
saved conversation. Restarting that runtime terminates its live processes and
invalidates their handles.

| Tool | Purpose |
| --- | --- |
| `command_exec` | Launch shell text with `cmd`, or a process without a shell with `argv`. |
| `python_exec` | Run Python in a managed subprocess. |
| `process_input` | Write non-empty input and collect a short response. |
| `process_read` | Wait for or page output from one or more processes. |
| `process_cancel` | Terminate a process tree and keep its final output. |

## Launch and wait budgets

Both execution tools take `mode: "auto"`, the default, or
`mode: "background"`. Auto waits for an initial window that the host sets. The
window is 30,000 ms in native Yoke and the server's `--default-yield-ms` on MCP.
Background returns at once. A process that finishes inside the window returns
its result immediately. Otherwise it keeps running under the returned
`session_id`, and you follow it with `process_read`.

| Operation | Default wait | Native maximum | MCP maximum |
| --- | --- | --- | --- |
| `command_exec`, `python_exec` in auto mode | 30,000 ms native, configured on MCP | set by the host | set by the host, capped by server configuration |
| `process_read` | 60,000 ms | 3,600,000 ms | `--max-remote-wait-ms`, at most 240,000 ms |
| `process_input` | 250 ms | 5,000 ms | 5,000 ms |

`process_read` and `process_input` accept a wait of zero. Negative waits are
invalid. The MCP server may set its read cap below 240,000 ms, so check the tool
schema. Execution calls have no wait field. Pick a longer wait on the next
`process_read` instead.

`python_exec.timeout` is an execution deadline in seconds. It can terminate
Python while a read is waiting. Running out of an initial wait or a
`process_read` wait never kills a process. A read that returns
`reason: "deadline"` is not a Python timeout. Check `timed_out`, `running` and
`exit_code` for the process's state.

Launch in the background:

```json
{"argv": ["uv", "run", "python", "job.py"], "mode": "background"}
```

Or let the host own the first wait:

```json
{"code": "print('ready', flush=True)", "mode": "auto", "timeout": 180}
```

## Read until completion, output or a snapshot

`process_read` takes these fields:

| Field | Meaning |
| --- | --- |
| `sessions` | Required list of 1 to 16 `{session_id, cursor?}` entries with distinct session IDs. |
| `until` | `"completion"` (default) or `"output_or_completion"`. |
| `wait_ms` | 60,000 by default, up to the host maximum. |
| `max_bytes` | 32,000 by default. 1,024 to 64,000 for the whole batch, not per session. |

In completion mode the read waits until every requested session has finished,
or until the single batch deadline passes. New output and a full byte budget do
not end the wait. In `output_or_completion` mode the read returns as soon as any
session has unread output or finishes. Use it when a program needs input before
it can finish. Errors such as unknown sessions or bad cursors return at once.
Items and errors keep the request order.

Pass back the `cursor` from each previous result:

```json
{
  "sessions": [
    {"session_id": 12345, "cursor": "pc1_AAAAAAAAMDkAAAAAAAAAAwAAAAAAAAAA"},
    {"session_id": 23456, "cursor": "pc1_AAAAAAAAWqAAAAAAAAAAAQAAAAAAAAAM"}
  ],
  "until": "completion",
  "wait_ms": 60000,
  "max_bytes": 32000
}
```

These cursor values are examples. Cursors are opaque. Copy them unchanged and
never decode, edit or build them. Omit a cursor to read from the earliest output
still retained. `wait_ms: 0` takes a snapshot in either mode, even for a
finished process. After a deadline, keep reading with the returned cursors.
Never relaunch the command to see its output.

The result is `{ok, reason, items}`. The normal reasons are:

| `reason` | Meaning |
| --- | --- |
| `completed` | In completion mode, every session finished. In output mode, at least one did. |
| `output` | Unread output ended an output-mode read. |
| `deadline` | The wait ran out first. |
| `snapshot` | A zero-wait read. |

`error` and `cancelled` are the exceptional reasons. The tools do not push
output. You read it explicitly.

## Completion notices

Native agents get a status notice before their next model call when a
background command finishes. A notice can cover several sessions and lists each
one's session ID, status, exit code, elapsed time, command and working
directory. It contains no output and does not move any cursor, so it cannot
replay logs you already read.

Read the output with `process_read` and the last cursor for each session. A
read that sees the session finished suppresses its later notice. A read made
while the session still runs does not. When the notice queue overflows, notices
report `older_events_dropped`.

MCP clients get no notices and must read explicitly.

## Input and cancellation

`process_input` requires `session_id` and non-empty `chars`. It also takes
`wait_ms` (default 250, 0 to 5,000), `max_output_tokens` and `cursor`.

```json
{
  "session_id": 12345,
  "chars": "yes\n",
  "cursor": "pc1_AAAAAAAAMDkAAAAAAAAAAwAAAAAAAAAA",
  "wait_ms": 250
}
```

The cursor marks output you have already read. It has nothing to do with where
input goes. Yoke checks that the cursor belongs to the target session before
writing anything. Without a cursor the response starts at the earliest retained
output, so it may repeat text you saw. It never skips unread output. After
writing, the call collects output for the short wait or until the process
exits. A call cancelled before the write writes nothing.

`input_written` is true once the write succeeds, false if Yoke did not try, and
null if an OS error leaves delivery uncertain. A failed response read does not
undo the input. Never resend input because the response read failed. Check the
process with `process_read` first. Yoke retries partial OS writes and stops on
cancellation while the write waits on a full stdin pipe. Delivery has its own
five-second limit before the response window starts. If delivery is cut short,
`input_bytes_written` reports how many bytes the OS accepted. Yoke does not
resend the rest.

Empty `chars` is invalid. To poll, use `process_read`. For a program waiting
on more input, read with `until: "output_or_completion"` or take a snapshot
instead of waiting for completion.

`process_cancel` takes `session_id` and terminates that process tree. It is
safe on a session that already finished, and the final output stays readable.
On MCP it also revokes the Python bridge capability and cancels the script's
managed calls. Cancelling returns no new cursor and erases no output. Read the
final output with the last cursor you had.

When the runtime is at its process limit, a new launch fails. Yoke never
terminates existing work to make room. Finish or cancel a process first.
Session IDs are not reused within one runtime. Cancelling a launch that is
still waiting for a slot stops it from starting.

## Results, paging and retention gaps

For execution, `ok` describes the process outcome. For reads and input, `ok`
describes the operation, so a successful read can report a nonzero exit code.
Always check `exit_code`. It is present on every item and null while the
process runs.

A process item has `session_id`, `running`, `exit_code`, `timed_out`,
`output`, `elapsed_seconds`, `cursor`, `has_more_output` and `gap`. Output is
decoded terminal text with carriage returns and final newlines kept as they
were. Execution and input results return one item plus `reason`. `next_tool`
is `process_read` while the process runs or has more output.

A cursor is a short versioned string such as `pc1_...`. Reads do not consume
output and can be repeated while the history is retained. Advance with the
returned `cursor` to get the next page. Reusing an old cursor repeats output.
A malformed cursor, or one from another session, is an error.

`has_more_output` is independent of `running`. After exit, timeout or
cancellation, keep reading with the returned cursor until you have the output
you need. A finished process can still take several snapshot pages.

Retention is bounded. When Yoke discards old output, the next read reports it
in `gap`, and the cursor moves past the lost range only with that report.
Mention the loss when it matters. A gap does not mean the missing output was
empty. Finished histories stay readable until the runtime evicts them. After
that, the handle returns an error and never restarts work.

Saved results from before this contract keep their original form for the
model. Resuming a conversation does not rewrite old tool results.

## Breaking migration

The old tool names are not advertised and do not work as aliases, in native
Yoke or on MCP. Update allowlists, callers and generated requests:

| Old interface | Replacement |
| --- | --- |
| `exec_command` | `command_exec` |
| `exec_python` | `python_exec` |
| `write_stdin` or `process_io` with input | `process_input` with non-empty `chars` |
| Polling with empty input through `write_stdin` or `process_io` | `process_read` with a `sessions` list |
| `yield_time_ms` or `wait_ms` on execution | `mode` on launch, then `process_read` with `wait_ms` |
| `next_cursor` from the old MCP reader | `cursor` on each process item |
| Structured cursor fields such as `after_seq` and `offset` | The opaque `cursor` string, copied with its `session_id` |
| `recommended_wait_ms` | An explicit `wait_ms` within the host limit, or the default |

The Python classes `ExecCommandTool`, `CommandTool` and `PythonExecTool` still
exist, and `CommandExecTool` is an alias of the command class. Importing them
does not bring back the old tool names. See [sdk.md](sdk.md) for the tool
classes.
