# Writing a daemon client

This page is for developers building a long-lived client of the Yoke daemon,
such as a terminal UI, a browser or a bridge like `yoke acp`. It covers
what such a client does beyond the requests in the [HTTP API guide](http.md):
finding or starting the daemon, handling version skew, attaching with its own
environment and tool policy, rendering events and reconnecting. Yoke's CLI and
ACP bridge follow these rules.

## Find or start the daemon

In Python, call `yoke.daemon.client.connect_or_spawn()`. It returns a handle
with the endpoint, pid, build and an optional skew warning, and it applies the
policy in the next section. A client in another language repeats its steps:

1. On Linux and macOS, try the socket at the default path, then the `socket`
   recorded in `~/.yoke/daemon.json` if that differs. The recorded path helps
   when your `XDG_RUNTIME_DIR` differs from the daemon's. On Windows, read
   `port` and `token` from `daemon.json`. A candidate counts only if
   `GET /health` answers.
2. If nothing answers, start `python -P -m yoke serve --daemon` detached. On
   POSIX it runs in a new session. On Windows it uses the `DETACHED_PROCESS`
   and `CREATE_NEW_PROCESS_GROUP` flags. Its working directory is `~/.yoke`,
   and its output goes to `~/.yoke/run/daemon.log`.
3. Poll for up to five seconds. If several clients start a daemon at once, one
   wins the lock and the others exit with status 0, so polling finds the
   winner.
4. Read `GET /daemon` and apply the skew policy below.

On Windows, and after any restart, read the port and token again. A replacement
daemon has a new token and may have a new port.

A spawned daemon does not inherit your shell environment. It receives only
these variables:

- `HOME`, `USERPROFILE`, `PATH`, `LANG`, `LC_*`, `TMPDIR`, `TEMP`, `TMP`,
  `XDG_RUNTIME_DIR`, `SYSTEMROOT` and `COMSPEC`.
- `YOKE_DB_PATH`, `YOKE_SESSION_DIR`, `YOKE_HTTP_SESSION_DIR`,
  `YOKE_DAEMON_PORT` and `YOKE_DAEMON_IDLE_GRACE`. The spawner resolves the
  three path variables against your current directory, because the daemon runs
  in `~/.yoke`.
- `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`,
  `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE` and `YOKE_DISABLE_TLS`, in upper or
  lower case. Provider connections read these from the daemon's own
  environment, so a daemon started behind a proxy or a private CA keeps
  working.
- `YOKE_DAEMON_BUILD_ID` and `YOKE_DAEMON_PROTOCOL`, which tests use to
  simulate skew.

The spawner removes activated virtualenv and Conda `bin` or `Scripts`
directories from `PATH`. Provider API keys, `YOKE_HTTP_TOKEN` and any other
`YOKE_*` variable stay behind. Each client passes its own environment when it
attaches, as described below.

`GET /daemon` reports the database path the daemon uses. If your client asks
for a different database, through `YOKE_DB_PATH` or `YOKE_SESSION_DIR`,
discovery fails with an error that names both paths instead of attaching to the
wrong database. Stop the running daemon or unset the variable.

## Version skew and restarts

Every process computes a build ID when it starts and keeps it. For a release
it is the version, such as `3.34.1`. For a git checkout it adds the commit and,
when the tree has changes, a hash of the tracked diff and untracked file
contents, for example `3.34.1+g8bc4e1d31613.dirty.5c1e0a9b2d4f7e31`. Two
different sets of edits produce different IDs. The hash is only computed when
the git checkout actually contains this Yoke package, so Yoke installed in
another project's virtualenv reports its plain version. The protocol version is
`3`. It changes whenever a client and daemon on either side of the change would
disagree about the wire format or required fields.

During discovery the client also reads the version that a newly started daemon
would load from disk right now, cached for two seconds. Then it decides:

| Situation | Action |
| --- | --- |
| Same build and protocol | Attach. |
| Daemon idle, and the version on disk is a newer release than the daemon's | Restart the daemon once, then attach to the replacement. |
| Otherwise, same protocol | Attach and show one warning. |
| Otherwise, different protocol | Refuse to attach. |

Only a strictly newer release version triggers a restart. Two checkouts at the
same version with different commits or edits never restart each other's daemon;
they attach with a warning when their protocols match and otherwise refuse.
Protocol-2 clients refuse a protocol-3 daemon, and protocol-3 clients refuse a
protocol-2 daemon. The error asks the user to upgrade or restart the older
client or daemon. Discovery can restart an idle daemon once when a newer release
is on disk, but it never attaches across protocols or repeats that restart.
After editing Yoke without bumping its version, run
`yoke daemon restart` to load your changes.

The client restarts with `POST /daemon/restart`, waits for the old pid to exit,
and starts a new daemon if no other client has. Discovery follows explicit
steps: probe, spawn if needed, status, decide, restart once, then attach or
refuse. A restart attempt is consumed before the client re-probes, even if
another checkout wins the replacement lock or an admission delays the restart.
Each discovery takes at most ten steps and requests at most one restart.

A restart response reports `scheduled: "when_idle"` while the request is
pending. The daemon watcher checks work again before committing to exit. If a
turn was admitted between the status read and the restart, the daemon refuses
with `409 daemon_busy`. If work arrives after the request, the client waits up
to its discovery timeout, then re-reads status and applies the warn or refuse
rule. A delayed restart does not make compatible discovery fail.

Skew handling never interrupts another client's turn, and a client that
attaches with a warning does not schedule a restart. Run
`yoke daemon restart --when-idle` to restart once work drains. Open leases do
not block a restart. Paused inputs and held queues do not count as work.

On Windows, a client checks that the daemon recorded in `daemon.json` is alive
and makes it prove its identity, bound to a fresh nonce and the recorded port,
before sending the bearer token. A stale record therefore never leaks the
token to another program on that port.

## Daemon lifecycle and work

`GET /daemon` reports `phase` as `serving`, `draining` or `exiting`.
`pendingExit` is `restart` or `stop` while draining; `exitReason` is `idle`,
`restart`, `stop` or `signal` once exiting. Stop takes priority over a pending
restart. Draining still accepts work. Exiting rejects new requests with
`503 daemon_exiting`, and the first exit reason stays fixed.

The additive `work` object explains `busy`. `runningSessions` lists sessions
with an active runtime phase or a worker still cleaning up. `runnableQueues`
lists sessions with unpaused inputs in an unheld queue. `heldQueues` lists
held queues for information; they never keep the daemon busy. `mutations`
counts admitted HTTP mutations still in flight. `admissionChanged` means an
admission started while the probe was reading work, so the daemon treats that
snapshot as busy and probes again before exiting.

An idle exit needs no work and no leases for the grace period. A requested
restart or stop waits for work but ignores leases. `yoke daemon status` shows
the phase and work reasons.

## Hold a lease

The daemon exits when it has no leases and no work. A client that stays open
holds a lease in one of two ways:

- `GET /event?sessionID=<id>&client=<name>` streams that session's events and
  holds a lease for as long as the stream is open. Each stream owns its lease.
- `GET /daemon/lease?client=<name>&pid=<pid>` holds a lease without events.

`yoke.daemon.lease.hold_lease()` keeps a lease open in Python. When the stream
ends, it rediscovers the daemon, starting one if needed, and opens a new lease
every half second until it succeeds. It returns when the daemon sends
`daemon.exiting` with reason `stop`, so `yoke daemon stop` is final until a
client starts the daemon again.

## Attach a client

An attachment tells the daemon who is driving a session and with what
environment and tool policy. Send it with `PUT /session/{id}/client`, or as
`client` in `POST /session` to create and attach in one request:

```json
{
  "owner": "terminal-6f1c2a",
  "kind": "cli",
  "mode": "full-access",
  "env": {"PATH": "/usr/bin:/bin", "HOME": "/home/me", "OPENAI_API_KEY": "sk-..."},
  "cwd": "/home/me/project"
}
```

| Field | Meaning |
| --- | --- |
| `owner` | Your client's identity, unique per running client. 1 to 128 characters. |
| `kind` | `cli`, `web` or `acp`. Defaults to `acp`. |
| `mode` | Tool policy. Defaults to `full-access`. |
| `env` | The complete environment for this client's turns. |
| `cwd` | Absolute existing directory where commands start. Defaults to the session directory. |
| `mcpServers` | MCP servers to add for this client's turns, for every kind. |
| `additionalDirectories` | Extra absolute existing directories the client authorizes, for every kind. |

Every kind registers tools for its `mcpServers`, even when the user's MCP
configuration does not enable MCP. Client servers replace saved servers with
the same name. Additional directories add a system instruction listing the
authorized paths. They must be absolute existing directories; the daemon
resolves them and removes duplicates. These fields stay in memory and follow
the input's admitting attachment.

An `acp` attachment also adds `update_plan` and `request_user_input`, plus a
system instruction in plan mode. These tools stay ACP-only because the bridge
delivers plan notifications and question forms to the client. The CLI and web
client do not provide that interaction contract. Attachments that omit
`mcpServers` and `additionalDirectories` keep their existing tools, ordering and
instructions. The kind given at session creation is saved as the session's
origin. Creating a session without `client` records `web`. Usage records carry
the kind of the attachment that admitted each input.

`mode` applies to every kind:

| Mode | Behaviour |
| --- | --- |
| `full-access` | Run every tool without asking. |
| `approval-required` | Run read-only tools. Ask through a permission request before anything else. |
| `auto-accept-edits` | Like `approval-required`, but also run the file edit tools `write`, `edit` and `apply_patch`. |
| `plan` | Run read-only tools. Block everything else until the client changes mode. |

The read-only tools are `read`, `extract_file_context`, `rg`, `fd`, `grep`,
`find`, `ls`, `web_fetch`, `web_search`, `web_research`, `attach_image`,
`mcp_inspect`, `process_read`, `skill`, `update_plan` and
`request_user_input`. Modes are a tool policy, not an operating-system sandbox.
A mode change applies to new tool calls and does not stop background work.
CLI clients use `full-access` and handle confirmation in the terminal.

Attachments coexist by owner. Sending `PUT` again for the same owner replaces
that owner's configuration, and inputs already admitted keep the configuration
they were admitted with. `GET /session/{id}/client?owner=<owner>` returns that
client's `mode`, latest `plan` and the list of attached `owners`. Without
`owner` it describes the most recently attached client.
`DELETE /session/{id}/client?owner=<owner>` removes only that owner. Attaching
and detaching work while a turn runs. They do not interrupt work or change
another client's tools, policy or environment.

Attachments live only in daemon memory. Relocating the session clears them.
A daemon restart loses them, and the daemon never stores credentials in
SQLite, so clients must attach again after reconnecting.

## Choose the admitting client

Pass your `owner` as `clientID` on `POST /session/{id}/prompt` and on each
entry of a queue patch's `admissions`. The daemon records that attachment as
the input's owner before any other request can start it. The owner decides the
environment and tool policy for the input's turn, even if the client later
detaches or reconfigures. An idempotent replay keeps the first admission's
owner.

| `clientID` | Owner |
| --- | --- |
| An attached owner | That client. |
| An owner that is not attached | Rejected with `409 client_not_attached`. |
| Omitted, as the browser does | The most recently attached client still attached. |
| Omitted, after every client detached | The last attached environment, without that client's approval mode or extra tools. |
| Omitted, never attached | The daemon's own environment. |

Every client watching the session receives `session.prompt.admitted` with
`inputID`, `clientID`, `delivery` and the full `prompt`. Record the IDs of
inputs you submit before you send the request, and skip those IDs when other
events arrive, so you do not render your own prompt twice.

## Environment

The attachment's `env` is the base environment for the owner's turns. The
daemon loads the project's `.env` into a copy and lets your values win. It
never writes to its own `os.environ`. It does not add `YOKE_HEADLESS`, which
belongs to the client that chooses headless output. A value you pass, such as
`YOKE_HEADLESS` itself, reaches tools and child Yoke processes unchanged.

Where the environment is used:

- `command_exec` and `python_exec` start in the attachment's `cwd` with a copy
  of the environment. Python interpreter selection reads that environment's
  virtualenv.
- MCP servers inherit the permitted base names and any names listed in
  `envVars` from the client environment, then apply their own `env`. Other
  client credentials and the daemon's private environment are not inherited.
  HTTP MCP servers send only their supplied `headers` and the MCP protocol
  headers, including the negotiated `MCP-Protocol-Version`. Server environment
  values and headers never appear in attachment status responses or saved
  attachment configuration. Tool output remains transcript content.
- Provider construction, model catalogs, model switching and automatic titles
  resolve credentials from the client environment. Credentials saved with
  Yoke's provider login commands fill in missing values. The daemon's own
  process keys are not merged into an environment you supplied.
- Provider plugins and repository tools that read `os.environ` directly still
  see the daemon process's environment.

A session without any attachment, including one driven only by the browser,
uses a copy of the daemon's environment plus the project's `.env`. For an
auto-started daemon that means the allow-listed variables above, project
values and saved credentials. A daemon started by a service manager keeps its
service environment, including provider keys from an `EnvironmentFile`.

An equivalent environment produces the same provider requests as running the
CLI directly. Attaching does not change message content, tool-result order,
cache scope or compaction format.

## Privacy

The daemon keeps `env` and `cwd` in memory. They are not session or admission
fields, and they never appear in client status responses or events.

Transcripts are user content and are stored without sanitizing. Messages, tool
results and compaction summaries keep the exact text the provider saw, so a
resumed session has the same history and cache identity. Message, tool and
prompt events mirror that content. A tool that prints an environment value has
put that value in the conversation.

Diagnostics are sanitized. The daemon registers values of at least eight
characters from environment variables whose names contain `key`, `token`,
`secret`, `password`, `credential` or `auth`, plus known provider credential
names. It replaces those values in tool inspector output, provider debug files,
API error messages, startup warnings, warning, error and retry events, error
fields on other events, and its own log formatters, including tracebacks.
Ordinary values such as `SHLVL=1` and paths stay intact. Values from detached
clients stay registered until the daemon exits, so late diagnostics from queued
work are still covered. Sanitizing never changes the transcript or provider
requests.

Request validation errors omit the request body and rejected values, so an
invalid attachment cannot echo its environment back.

## Event names

Agent progress reaches clients as live SSE events. The payload keeps the
agent's snake_case keys and adds `turnID` and `inputID`, except for
`session.context.updated`. A client that already renders agent callbacks can
map the names back:

| Agent callback | SSE event | Payload |
| --- | --- | --- |
| `model_start` | `session.model.started` | iteration, provider and model timing metadata |
| `model_end` | `session.model.ended` | iteration, usage, duration, provider and model metadata |
| `provider_retry` | `session.provider.retry` | provider, model, attempt, max_retries, delay_seconds, message |
| `provider_rate_limited` | `session.provider.rate_limited` | the same fields as a retry |
| `provider_recovered` | `session.provider.recovered` | provider, model, attempts |
| `notice` | `session.notice` | notice fields |
| `warning` | `session.warning` | warning fields |
| `error` | `session.error` | error fields |
| `assistant_message` | `session.message.updated` | content, phase and message metadata |
| `tool_execution_start` | `session.tool.started` | tool_name, tool_call_id, tool_arguments, iteration |
| `tool_execution_output_delta` | `session.tool.delta` | tool_call_id, tool_name, delta and output cursor fields |
| `tool_execution_end` | `session.tool.ended` | tool_name, tool_call_id, ok, error, result or output, timing |
| `compaction_summary_start` | `session.compaction.summary.started` | estimated_input_tokens and progress fields |
| `compaction_summary_end` | `session.compaction.summary.ended` | ok, duration_seconds, response_chars, error |
| `context_compaction` | `session.context.compacted` | compaction counts, limits and reason |
| `compaction_start` | `session.compaction.started` | callback fields |
| `compaction_progress` | `session.compaction.delta` | callback fields |
| `compaction_end` | `session.compaction.ended` | callback fields |
| `context_usage` | `session.context.updated` | token and limit fields plus the normalized snapshot |

Token counts survive secret sanitizing. Events for a turn that was already
interrupted are not published.

When a turn ends, the daemon publishes durable events in this order:

| How the turn ended | Durable events |
| --- | --- |
| The agent returned a result | `session.prompt.settled` with outcome `completed` or `stopped`, then `session.message.updated` with the saved `leafID` |
| The agent raised an error | `session.prompt.settled` with outcome `failed`, then `session.runtime.failed` with the error |
| The workspace check failed before the turn started | `session.runtime.failed` only. The input goes back to the queue paused, unsettled. |
| The turn was interrupted | `session.prompt.settled` with outcome `stopped`, then `session.interrupted` with `reason` set to `user`, `steering` or `shutdown` |

A client waiting for one input should therefore stop on
`session.message.updated`, `session.runtime.failed` or `session.interrupted`
for that `inputID`.

## Reconnect

Follow the catch-up steps in the
[HTTP API guide](http.md#stream-events-and-recover-after-a-disconnect) on every
connect and after `server.resyncRequired`. When the stream drops:

1. Run discovery and the skew policy again. The daemon may have restarted on a
   new port.
2. Attach again with a fresh copy of your environment.
3. Open the filtered event stream, then catch up from history and the
   transcript, deduplicating durable events by `seq`.

Any full stream queue, including a live-delta overflow, sends
`server.resyncRequired` and closes. Refresh the input and runtime snapshots to
replace stale queue and activity state. Discard pending live deltas before
applying those snapshots.

Never retry an ambiguous write automatically. If a prompt admission timed out,
retry it only with the same input ID, so the daemon can return the original
receipt. Treat other mutations the same way. Read state first, then decide.

After a daemon restart, inputs admitted before the crash lose their private
owner. They run with a newly attached client's environment or with the daemon's
fallback environment.

## Capacity

The daemon executes up to 50 sessions at once on a pool of 54 worker threads,
with a separate pool of four threads for storage. Most of a turn is spent
waiting on the provider's network responses, which is why the limit is high.
Excess turns wait in FIFO order with `activity: "Waiting for a slot"` and state
`running`. Attached idle clients use no slots.

## Images from terminal clients

A prompt may set `imageProjection: "cli"` on image attachments. The daemon
then builds the provider message in the terminal's format. Image labels restart
at one for each prompt, and images carry no attachment name. Without it, the daemon uses the browser and ACP projection.
The flag is saved with the input and survives queue edits and recovery.


## Observe input phases

Protocol 3 is current. Clients require `features.inputLifecycle`, `globalEvents`,
`promptAdmission`, `steering` and `queueEditor`. Missing features or a different
protocol fail attachment with an upgrade and restart message. Load `GET /session/{id}/input?live=true` for `inputs`, `held` and
`inputRevision`. Input phases are `queued`, `paused`, `promoted`, `running`,
`settled` and `removed`. `pauseCause` records `user` or `workspace`.

Apply `session.input.changed` in `inputRevision` order. Each transition includes
the input's phase and identity. The revision survives daemon restarts. Refresh
the snapshot on a gap. Clients use these phases for input placement and completion.

A held queue has a reason such as `interrupt`, `finalization_failed`,
`selection_failed`, `daemon_restart` or `workspace_unavailable`. Display the
reason and offer `POST /session/{id}/queue/resume`. An admission or patch with
`resume: true` also clears the hold. Resume preserves individual pauses.

Cancel a specific input through `POST /session/{id}/input/{inputID}/cancel`.
This removes a queued or paused input without stopping another client's turn.
It stops a promoted or running input only when that input owns the turn.
Terminal inputs return `409 input_settled`. Read one input, including its
terminal outcome, with `GET /session/{id}/input/{inputID}`.

The prompt receipt includes `phase` and reflects the state after wake. A lease
connection callback runs after the daemon acknowledges the lease, so TCP clients
can refresh their endpoint before making requests against the replacement.

The daemon also holds an idle queue with reason `manual` when a request uses
`resume: false`. Hold changes publish `session.queue.held` with `held` and
`inputRevision`. Holds do not change the queue edit revision.
