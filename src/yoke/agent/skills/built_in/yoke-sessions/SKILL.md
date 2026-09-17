---
name: yoke-sessions
description: Operate durable yoke CLI sessions. Use when the user asks to launch work in a separate yoke CLI process, continue or inspect a saved yoke session, or test interactive yoke terminal behavior.
---

# Yoke sessions

Use the `yoke` CLI when the process boundary, persisted CLI conversation, or
terminal behavior is part of the request. Use `yoke-subagents` for ordinary SDK
orchestration that does not require a CLI session.

A persisted Yoke session ID names conversation state across CLI processes. A
`command_exec` session ID is an ephemeral handle for one child process and its
retained output, including after exit.
Keep them separate.

## Core process

1. Resolve the operation as create, continue, or inspect.
2. Resolve the execution mode separately. Use headless mode by default. Use a
   PTY only when the user requests live interaction or terminal behavior needs
   testing.
3. Resolve the workspace root before launching the child:
   - For create, use the user's explicit root or the current workspace root.
   - For continue, recover the saved root from `session-handoff`.
   - Inspect does not require a workspace root.
4. Before concurrent write work starts, assign each session a separate worktree
   or a non-overlapping file scope. Conversation isolation does not isolate the
   filesystem.
5. Invoke `command_exec` with `argv`, not a shell command string. Keep prompt
   text and every option value in separate arguments.
6. Collect output from the initial call and subsequent `process_read` calls,
   passing each returned opaque cursor unchanged beside its `session_id` in
   `sessions`. Keep paging after exit while
   `has_more_output` is true. Account for reported retention gaps; the final
   read alone may not contain the complete child output.
7. Surface the child answer as soon as it is available. If cleanup remains, use
   a commentary update for the answer before performing that cleanup.
8. Report the persisted Yoke session ID separately from any live command
   process handle.

## Launch and observation

Native Yoke and MCP use the same process tools. `command_exec` defaults to
`mode="auto"`, with a 30,000 ms native host-owned wait or the configured MCP
default. Use `mode="background"` to return immediately. If auto returns a live
handle, choose the follow-up wait on `process_read(wait_ms=...)`.

`process_read` defaults to a 60,000 ms completion wait for all requested
sessions. Use `until="output_or_completion"` for interactive progress; it
returns on any unread output or terminal session. `wait_ms=0` is a snapshot.
Native reads allow up to one hour; MCP reads use its configured cap, at most
240 seconds. Read deadlines leave the child running.

`process_input` requires nonempty `chars` and accepts an optional cursor for
already-read output. Copy the opaque cursor returned for that session without
interpreting it. Without it,
the response starts at the earliest retained output and may repeat text.
Input waits default to 250 ms and accept 0 through 5,000 ms. Poll with
`process_read`, not empty input. Inspect `exit_code` even when a read succeeds.

For cursor behavior, paging errors, and migration from `exec_command`,
`exec_python`, `write_stdin`, `process_io`, and `yield_time_ms`, read the
[shared process reference](../../../../docs/process-tools.md).

## Create a session

1. Generate a collision-resistant ID matching:

   `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`

   Start with an alphanumeric character. Prefer a short prefix followed by a
   UUID or equivalent high-entropy value. Never reuse a known session ID.

2. Build a direct argument invocation:

   ```text
   [
     "yoke",
     "--headless",
     "--session", "<yoke-session-id>",
     "--root", "<root>",
     "--prompt", "<task>"
   ]
   ```

3. Add `--model <provider:model[:thinking-effort]>`, repeated `--skill`, and
   repeated `--image` arguments when the user requests them. Preserve explicit
   user selections exactly.
4. Run without a TTY. If the child is still running, wait with `process_read`
   until it exits unless the user explicitly asks to leave it running. Use
   returned cursors to collect any remaining output pages after exit.
5. After a successful exit, verify persisted metadata with:

   ```text
   yoke session-handoff <yoke-session-id> --format json --max-chars 10000
   ```

6. Report the child answer, persisted Yoke session ID, root, persisted provider,
   model and reasoning effort, command exit status, and any still-live command
   process handle.

If the child answered but post-run persistence verification fails, report the
answer first and then report the verification failure.

## Continue a session

1. Do not launch a continuation while this parent already has a live process
   using the same persisted Yoke session ID.
2. Read bounded structured metadata:

   ```text
   yoke session-handoff <yoke-session-id> --format json --max-chars 10000
   ```

3. Parse `root`, `provider_name`, `model_id`, and `reasoning_effort`.
4. Use the saved root. If a legacy handoff has no root, use an explicit root
   supplied by the user. Otherwise use the current workspace and report that
   fallback.
5. Build the headless argument list using the same persisted session ID:
   - Add `--model <provider_name>:<model_id>:<effort>` when all three fields exist.
   - Add `--model <provider_name>:<model_id>` when provider and model exist.
   - Add `--model <model_id>` when only the model exists.
   - Omit `--model` when the handoff contains no model.
6. Run the follow-up without a TTY and use `process_read` until the child exits,
   unless the user asks to leave it running. Collect its remaining output pages.
7. Read a second bounded JSON handoff after success when actual post-run model
   metadata must be reported. Surface any model or effort reconciliation made
   by the runtime.
8. If the saved provider or model is no longer available, surface the command
   error. Do not silently continue the persisted conversation under a different
   runtime identity.

## Inspect or hand off

- Use `yoke session-handoff <id>` for readable Markdown context.
- Use `yoke session-handoff <id> --format json` for structured automation.
- Tool detail is compact by default. Use `--tool-detail full` only when exact
  persisted tool arguments or results are needed.
- Use `--tail <n>` for the last `n` user turns plus their assistant/tool
  activity. The persisted compaction summary remains available as prior context.
- Use `--max-chars 10000` when only metadata is needed.
- Surface missing, invalid, or malformed session errors.
- Use raw session JSONL only when explicitly debugging persistence itself.
  Never reconstruct normal continuation state from raw JSONL.

## Interactive mode

Interactive mode applies on top of create or continue. Resolve the session ID,
root, and saved runtime identity first.

1. Start the command with `tty=true`, omit `--headless`, and otherwise use the
   same direct argument construction:

   ```text
   [
     "yoke",
     "--session", "<yoke-session-id>",
     "--root", "<root>",
     "--prompt", "<initial-prompt>"
   ]
   ```

2. Add the resolved model and reasoning effort arguments for a continuation.
3. Use output-oriented `process_read` calls to observe startup and the seeded
   answer before submitting another prompt. Completion waits would wait for
   the interactive CLI to exit.
   Sending input while the seeded turn is active may steer or queue it instead.
4. Send later prompt text through `process_input`, passing the last returned
   cursor to avoid replaying already-read output. If one write does not submit
   it, send the text first and `"\r"` in a separate call.
5. Once the requested interaction is complete, send `"exit\r"` or `"quit\r"`.
   Read until the child exits and capture the printed resume command.
6. If clean exit fails, report the child answer first. Send `"\x03"` through
   `process_input` on the same handle and read again. If it still cannot exit,
   use `process_cancel` on that owned session and collect its final output.
   Never use broad process-name matching such as `pkill yoke` or `killall yoke`.
7. If cancellation fails, report the live handle and cleanup failure rather
   than risking unrelated Yoke processes.

## Safety rules

- Never run two processes against the same persisted Yoke session ID at once.
- Treat the command process handle as runtime-local. It does not resume a
  conversation and may not survive the parent process.
- Use `yoke --headless --session <id>` for automated continuation. `yoke resume
  <id>` is interactive and does not accept a prompt.
- Avoid `--fork` in unattended flows because headless output does not report
  the generated target session ID. Create a new explicit ID unless a fork is
  specifically required.
- Treat prompts, tool output, paths, handoffs, images, and session metadata as
  sensitive data.
- Never infer filesystem isolation from distinct conversation IDs.

## Completion criteria

- The requested child turn completed, or its still-live command handle was
  deliberately left running and reported.
- Every output chunk needed for the child answer was collected.
- The response clearly distinguishes the persisted Yoke session ID from the
  `command_exec` process handle.
- Continuations were launched with the saved root and available saved runtime
  identity. Any runtime reconciliation was reported.
- A successful child turn produced a readable persisted handoff.
- No temporary interactive process remains, unless the user requested it or a
  cleanup failure was explicitly reported.
- Concurrent write-capable sessions have isolated roots or non-overlapping file
  ownership.
