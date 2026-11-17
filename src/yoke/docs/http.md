# HTTP API

This guide is for developers who drive Yoke from another program over HTTP. It
explains how the per-user daemon, sessions, turns and events work, then walks
through the common tasks with real requests. The
[HTTP API reference](http-reference.md) lists every endpoint with its fields
and error codes. [Writing a daemon client](daemon-client.md) covers what a
long-lived client such as a terminal or browser adds on top: discovery,
attachments, per-client environments and version skew.

All paths below have the `/api/v1` prefix. Field names are camelCase.

## Per-user daemon

Each user runs one daemon, `yoke serve --daemon`. The CLI and `yoke acp` start
it on demand, so you rarely start it yourself. The daemon owns every saved
session, runs all turns, and serves the browser UI on its TCP port. To open the
UI, visit `http://127.0.0.1:<port>/#token=<token>` with the values from
`daemon.json`. Sessions live in one SQLite
database, `~/.yoke/yoke.db` by default.

The daemon listens in two places:

| Listener | Platform | Authentication |
| --- | --- | --- |
| Unix socket `$XDG_RUNTIME_DIR/yoke/daemon.sock` | Linux, macOS | The kernel-reported peer uid must match the daemon's user. No token. |
| TCP `127.0.0.1:8770` | All | Bearer token from `~/.yoke/daemon.json`. |

If `XDG_RUNTIME_DIR` is unset, or the socket path would exceed the Unix limit
(107 bytes on Linux, 103 on macOS and the BSDs, both excluding the terminating
NUL), the socket moves to `~/.yoke/run/daemon.sock`. If that path is also too
long, startup fails and you need a shorter `XDG_RUNTIME_DIR`. Windows has no
socket and uses TCP only.

If port 8770 is taken, the daemon logs a warning and binds an ephemeral loopback
port instead. An explicit `--port` or `YOKE_DAEMON_PORT` is strict. If that port
is taken, startup fails. Port `0` asks for an ephemeral port. Always read the
port from `daemon.json` rather than assuming 8770.

The daemon writes `~/.yoke/daemon.json` with mode 0600 after it binds its
listeners:

```json
{
  "pid": 41237,
  "build_id": "3.34.1+g8bc4e1d31613",
  "protocol_version": "1",
  "socket": "/run/user/1000/yoke/daemon.sock",
  "port": 8770,
  "token": "<bearer token>",
  "idle_exit": true,
  "started_at": 1795435602.5
}
```

The token is new on every start unless `YOKE_HTTP_TOKEN` is set in the daemon's
own environment. A daemon spawned by the CLI never inherits that variable. Do
not copy the token into logs or share it. Anyone holding it can read workspace
files, run commands and spend your provider credits.

The daemon takes an exclusive lock on `~/.yoke/daemon.lock` before it binds
anything. A second daemon that loses the race exits with status 0. After a
crash, the next daemon to take the lock replaces the stale socket and
`daemon.json`. The daemon sets `~/.yoke` and its run directory to mode 0700,
including directories that already exist. A detached daemon logs to `~/.yoke/run/daemon.log`, which rotates at
2 MiB and keeps two backups.

### Idle exit and leases

A lease is an open stream that keeps the daemon alive. `GET /event?client=<name>`
and `GET /daemon/lease?client=<name>` both hold one. The daemon exits about five
seconds after it has no leases and no work. Work means a running, stopping or
input-waiting turn, a worker still finishing a retired turn, or an unpaused
queued input in a queue that is not held. Paused inputs and held queues do not
keep the daemon alive.
`--no-idle-exit` turns idle exit off, for a daemon run by a service manager.
A sample systemd user unit is in
[`docs/yoke-daemon.service`](../../../docs/yoke-daemon.service).

When the daemon exits, it closes event streams, waits for workers, terminates
background processes started by tools, and removes the socket and
`daemon.json` before it releases the lock.

### Windows

On Windows the daemon listens on loopback TCP only, and every client sends the
bearer token from `daemon.json`. Before writing the token, the daemon replaces
the access control lists of `~/.yoke` and its run directory with protected
lists that grant access only to the current user. `daemon.json` gets the same
protection before it is moved into place. If the filesystem cannot enforce
these lists, startup fails. Use a local profile on NTFS. An administrator can
still take ownership of the files, as root can on Unix.

The lifetime lock is a `msvcrt.locking` byte lock that Windows releases when
the process exits, including after a crash. Stop and restart use the same HTTP
controls as on Unix. The daemon does not depend on Unix signals.

### Standalone server

`yoke serve` without `--daemon` runs a separate HTTP server that is not the
per-user daemon. It listens on `127.0.0.1:8765` by default, prints its URL and
bearer token at startup, and does not write `daemon.json` or exit when idle.
Use it for a dedicated session store (`--session-dir` or
`YOKE_HTTP_SESSION_DIR`), a stable token (`--token` or `YOKE_HTTP_TOKEN`), or a
network listener:

```bash
yoke serve --host "$(tailscale ip -4)" --allow-remote
```

A non-loopback `--host` requires `--allow-remote`. The bearer token is still
required, and your firewall or Tailscale ACLs decide who can reach the port.
Never expose the server to an untrusted network. `--open` opens the browser UI
with the token in the URL fragment, which browsers do not send to the server.
`--hide-token` suppresses the token line when a service manager supplies
`YOKE_HTTP_TOKEN`. `--verbose` turns on Uvicorn access logs.

The `/daemon` endpoints return `404 daemon_mode_required` on a standalone
server. Everything else in this guide works the same way.

### Find saved sessions

`GET /api/v1/session?search=SQLite%20migration&archived=false` searches titles
and user/assistant text with FTS5. Results come in relevance order with a short
`searchSnippet`; `<mark>` delimiters identify matching words. Render the rest
as plain text. Tool output and images are excluded. Retained branches are
searchable. All words must match one message or title. Quotes, asterisks and
other punctuation do not activate FTS query syntax. ID and directory substring
lookups remain available.

Keep `directory`, `archived`, `pinned`, `origin` and `search` unchanged when
following `cursor.next`. `total` counts all filtered matches. Without search,
`order` controls the list; with search it breaks relevance ties.

Session responses include `origin`, the creating client. Filter with
`origin=cli`, `web`, `acp`, `sdk` or `unknown`. Headless terminal sessions use
`cli`. Old imports without reliable creating-client metadata use `unknown`.
Later attachments do not change origin. On creation, `client.kind` wins;
without a client, `origin` may be supplied and otherwise defaults to `web`.
In-process SDK agents keep file snapshots and usage rows, without creating
session rows. An SDK client using this HTTP API can supply `origin: "sdk"`.
Protocol 3 is current.

### Authentication

Requests on the Unix socket need no credentials. Requests over TCP need this
header:

```text
Authorization: Bearer <token>
```

`GET /health`, `GET /openapi.json` and the browser application routes are
public. Every other `/api/v1` route returns 401 without a valid token:

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or missing bearer token.",
    "details": {},
    "requestID": "req_b7df5915851d45968fc2586995011b6e"
  }
}
```

The 401 response also carries `WWW-Authenticate: Bearer`.

## Sessions

A session is a saved conversation tree bound to a working directory. Its ID is
a string such as `20261117-120642-1d2dae`, chosen by the daemon unless you pass
one. A session does not depend on any HTTP connection. Listing sessions reads
summary rows from SQLite and does not load conversation history or construct
providers. The daemon loads a session's runtime only when an operation needs it.

`location.directory` is the saved working directory. `workspace` reports what
the daemon finds on disk now: `available`, `missing`, `not_directory`,
`unreadable`, `unconfigured` or `invalid`. A session whose directory is gone
can still be read, renamed, pinned and archived, and its queued inputs can be
listed and removed, but it cannot run turns, change model or compact until you restore or relocate the
directory. See [Recover a moved workspace](#recover-a-moved-workspace).

The web UI calls archiving "Settle". The API field is `archived`. Archiving
only affects listing, and sending a prompt to an archived session unarchives it
in the same request.

## Turns and input admission

Every prompt becomes an input with an ID. `POST /session/{id}/prompt` admits
the input. The daemon writes it, its `session.prompt.admitted` event and the
queue change in one SQLite transaction, then returns a receipt. Only after that
commit does the input become eligible to run. Admission and execution are
separate steps, so a successful response means the input is saved, not that the
model has answered.

Each session runs one turn at a time. The daemon picks the next input in this
order:

1. Unpaused inputs with `delivery: "steer"`, oldest first.
2. Unpaused inputs with `delivery: "queue"`, oldest first.

Paused inputs never start. When a turn finishes, the daemon starts the next
eligible input without waiting for a request.

The two delivery modes differ when a turn is already running:

| Delivery | Behaviour while a turn runs |
| --- | --- |
| `steer` (default) | The daemon interrupts the running turn, saves it as interrupted, and starts the steering input. |
| `queue` | The input waits until the running turn finishes. |

`resume` defaults to `true` and tells the daemon to look for work right after
admission. With `resume: false` the input is saved but nothing starts now. It
still runs when the current turn finishes, or on the next request that resumes.
Pause an input if you want to hold it.

`POST /session/{id}/interrupt` stops the running turn. The daemon saves the
partial turn with a `control` entry and settles the input with outcome
`stopped`. Interrupting does not start the next queued input. Queued inputs
wait until the next admission or queue edit with `resume: true`.

Closing an HTTP request or an event stream never cancels admitted work.

The daemon runs at most 50 sessions at once. A turn beyond that limit waits in
FIFO order. Its state stays `running` and its `activity` reads
`Waiting for a slot`. You can interrupt a waiting turn.

## Events

The daemon publishes two kinds of events:

- Durable events record state changes such as admission, promotion,
  settlement, interruption, queue edits and saved messages. The daemon writes
  each one to a per-session journal with a sequence number `durable.seq`,
  and publishes it on the live stream only after the write commits.
- Live events carry progress that is not saved: model start and end, tool
  starts, tool output deltas, provider retries, activity changes, permission
  requests and plan updates. They have `durable: null`.

`GET /event` is a Server-Sent Events stream. Each frame looks like this:

```text
id: evt_89abf37676b1bc673365a7a5
event: session.prompt.admitted
data: {"id":"evt_89abf37676b1bc673365a7a5","type":"session.prompt.admitted","time":"2026-11-17T12:06:42.623292+00:00","sessionID":"20261117-120642-1d2dae","location":{"directory":"/home/me/project"},"durable":{"aggregateID":"20261117-120642-1d2dae","seq":2,"version":1},"data":{"inputID":"inp_docs_001","clientID":null,"delivery":"queue","prompt":{"text":"Run the test suite and fix failures.","attachments":[],"continuation":false}}}
```

The first frame is always `server.connected`. The daemon sends a `: heartbeat`
comment after 20 seconds without events. Without `sessionID` the stream carries
every session's events. With `sessionID` it carries only that session's events
plus the global `server.resyncRequired` and `server.stopping` notices, and the
daemon filters before queueing, so a busy unrelated session cannot overflow
your stream.

Each stream has a queue of 512 events. Delivery follows these rules:

- If any event does not fit, including live progress, the daemon empties the queue, sends
  `server.resyncRequired` with `{"reason":"slow_consumer"}`, and closes the
  stream. No event after the gap reaches that stream before the notice.
- The stream does not replay missed events on reconnect. `Last-Event-ID` is
  ignored. Use `/history` instead.

Durable sequence numbers increase per session but can have gaps. To catch up
after connecting or after `server.resyncRequired`, follow
[Stream events and recover after a disconnect](#stream-events-and-recover-after-a-disconnect).

## Errors

Every error uses the same envelope. `requestID` matches the `X-Request-ID`
response header. If you send an `X-Request-ID` header, the daemon uses your
value.

```json
{
  "error": {
    "code": "input_identity_conflict",
    "message": "Input id was already used with different admission data.",
    "details": {"inputID": "inp_docs_001"},
    "requestID": "req_5b51f0c7ce464743af4245f0f1effa20"
  }
}
```

Request validation failures return 422 with code `validation_error` and
`details.errors`, a list of `{loc, type, msg}`. While the daemon shuts down,
every request returns `503 daemon_exiting`. Reconnect and retry after
rediscovering the daemon. The [reference](http-reference.md#error-codes) lists
every code.

## Connect to the daemon

On Linux and macOS, use the socket. The host part of the URL is ignored.

```bash
SOCK="${XDG_RUNTIME_DIR:-$HOME/.yoke/run}/yoke/daemon.sock"
curl --unix-socket "$SOCK" http://yoke/api/v1/health
```

Over TCP, read the port and token from `daemon.json`:

```bash
PORT=$(jq -r .port ~/.yoke/daemon.json)
TOKEN=$(jq -r .token ~/.yoke/daemon.json)
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/v1/health"
```

```json
{
  "data": {
    "healthy": true,
    "version": "3.34.1",
    "protocolVersion": "1",
    "buildID": "3.34.1+g8bc4e1d31613"
  }
}
```

If nothing answers, start the daemon with `yoke serve --daemon` or run any
`yoke` command that needs it. `yoke daemon status` prints the pid, build,
socket, port, leases and whether it is busy. The remaining examples use the TCP
form and assume `$BASE` is `http://127.0.0.1:$PORT/api/v1`.

## Create a session

```bash
curl -X POST "$BASE/session" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"location":{"directory":"/home/me/project"},"title":"Fix the flaky test"}'
```

```json
{
  "data": {
    "id": "20261117-120642-1d2dae",
    "title": "Fix the flaky test",
    "pinned": false,
    "archivedAt": null,
    "location": {"directory": "/home/me/project"},
    "workspace": {"status": "available", "message": null},
    "time": {
      "created": "2026-11-17T12:06:42.553963+00:00",
      "updated": "2026-11-17T12:06:42.553963+00:00",
      "lastUserMessage": null
    },
    "selection": {"provider": null, "model": null, "reasoningEffort": null},
    "contextUsage": null,
    "databasePath": "/home/me/.yoke/yoke.db",
    "contextWindowTokens": null,
    "tree": {"leafID": null, "entryCount": 0},
    "queue": {"total": 0, "steering": 0, "queued": 0, "paused": 0, "revision": 0}
  },
  "startup": null
}
```

`location.directory` must be an existing readable directory. If it is not, the
daemon returns `400 workspace_unavailable`. A session with no `selection` uses
the provider and model that the CLI would pick for that directory: project
settings, then user settings, then provider defaults.

Creating a session is idempotent when you supply `id`. Repeating the request
with the same `id` and directory returns the existing session. The same `id`
with a different directory returns `409 session_identity_conflict`.

Set `startup: true` to run the CLI's startup checks. The response then has a
`startup` object with `warnings`, `activeSkills`, `modelSource` and
`toolReport`. Passing `skills` also runs startup and activates those skills.
A startup failure returns `400 session_startup_failed` and leaves the empty
session in place, so you can correct the request and retry with the same `id`.
To create a session already attached to your client, include a `client` block.
[Writing a daemon client](daemon-client.md#attach-a-client) describes it.

## Send a prompt idempotently

Generate the input ID yourself and keep it until you know the outcome:

```bash
curl -X POST "$BASE/session/$SID/prompt" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"inp_docs_001","prompt":{"text":"Run the test suite and fix failures."},"delivery":"queue"}'
```

```json
{
  "data": {
    "id": "inp_docs_001",
    "sessionID": "20261117-120642-1d2dae",
    "prompt": {
      "text": "Run the test suite and fix failures.",
      "attachments": [],
      "continuation": false
    },
    "delivery": "queue",
    "phase": "queued",
    "admittedSeq": 2,
    "promotedSeq": null,
    "timeCreated": "2026-11-17T12:06:42.623534+00:00"
  }
}
```

If the request times out or the connection drops, send exactly the same request
again. The daemon compares the session, prompt text, attachments,
`continuation` flag and `delivery` with the saved input:

- If they match, it returns the original receipt and admits nothing new.
- If they differ, it returns `409 input_identity_conflict`.

`resume` and `clientID` are not part of that comparison. Without an `id`, the
daemon generates one, and a retry admits the prompt twice. Yoke's own clients
never retry an ambiguous write with a new ID.

Prompt text is limited to 1 MiB (`413 prompt_too_large`). Empty text without
attachments returns `400 empty_prompt`.

To ask the model to continue from the saved conversation without adding a user
message, send `{"prompt":{"continuation":true}}` with empty text and no
attachments. The session must already contain a user message
(`400 empty_continuation`), and continuation input with text or attachments
returns `400 invalid_continuation`. The flag is part of the idempotency
comparison.

## Runtime phases

The daemon owns one in-memory phase per loaded session. A turn reports
`turn_active`, a stop reports `retiring`, and model selection and manual
compaction report `operation_active`. A session with no logical work reports
`idle`, including when its legacy `state` is `error`.

`GET /session/active`, `/wait` and `session.active.changed` include `phase`,
`revision`, `inputID` and `operation`. The operation value is `{id, kind}` with
kind `selection` or `compaction`, or null. Each phase or activity change increases
`revision` within the loaded runtime. Refresh snapshots after reconnecting,
since a restarted daemon starts a new revision sequence. Permission and question
waits report `state: "waiting_input"`. Model selection reports `state: "running"`
while it owns the session.

A failed stop write clears the active turn, reports `error` and keeps late
worker output fenced. The daemon leaves queued work stopped after this failure,
as it does after a successful user stop. A failed compaction-start journal write
leaves the session idle. A compaction task failure or cancellation clears its
operation and publishes the error. Cancellation waits for an already running
worker to finish before admitting another mutation.

`POST /interrupt` leaves operations running and returns `interrupted: false`
with `reason: "operation_not_interruptible"`. Clients can use `operation` to
hide the turn Stop action during manual compaction or model selection.

Runtime state, permission and question events stay live-only. If a subscriber
cannot keep up, the daemon sends `server.resyncRequired` and closes the stream.
Reconnect and refresh snapshots instead of relying on a missing state event.

## Wait for the answer

`POST /session/{id}/wait?timeoutMs=60000` blocks until the session has no
running turn or operation, then returns its runtime state:

```json
{
  "data": {
    "state": "idle",
    "turnID": null,
    "startedAt": null,
    "activity": null,
    "error": null
  }
}
```

`timeoutMs` accepts 1 to 300,000. If the timeout passes first, the response is
still 200 and reports the current state, for example `running`. Omit
`timeoutMs` to wait without a limit. `state` is one of `idle`, `running`,
`stopping`, `waiting_input` or `error`. `error` means the last turn failed, and
`error` holds the runtime failure message. Runtime snapshots also include the
same value in `lastError`.

Wait observes the session, not one input. If other inputs are queued behind
yours, the session stays busy until they finish too. To follow a single input,
watch `session.input.changed` for `phase: "running"` when it starts and
`phase: "settled"` when it ends, with `outcome` set to `completed`,
`stopped`, `failed` or `recovered`.

Read the answer from the transcript. `GET /session/{id}/message` returns the
active branch newest first. Assistant messages carry the `inputID` of the turn
that produced them, and the final reply has `phase: "final_answer"`:

```json
{
  "data": [
    {
      "type": "assistant",
      "id": "3f9c2a7d10b84e6c",
      "kind": "assistant",
      "inputID": "inp_docs_001",
      "phase": "final_answer",
      "content": [{"type": "text", "text": "All 214 tests pass now."}],
      "toolCalls": [],
      "timeCreated": "2026-11-17T12:09:10.120000+00:00",
      "turnSummary": {"durationSeconds": 148.2, "toolCount": 9}
    }
  ],
  "cursor": {"previous": null, "next": "<opaque>"},
  "snapshotSeq": 31
}
```

`turnSummary` appears on the last entry of turns that ran for at least one
minute. `snapshotSeq` is the newest durable event sequence included in this
read. Pass it as `after` to `/history` to receive only later changes.

## Stream events and recover after a disconnect

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "$BASE/event?sessionID=$SID&client=my-tool"
```

`client` names your stream and makes it a daemon lease, so the daemon does not
idle out while you watch. Omit it if you do not want to keep the daemon alive.
An unknown `sessionID` returns 404.

A client that wants a complete view does this on every connect, and again
after `server.resyncRequired`:

1. Open `GET /event` first, so you do not miss events published while you
   catch up. Buffer what arrives.
2. Read current state: `GET /session/active` for running sessions, then
   `GET /session/{id}`, `/queue` and `/message` for the sessions you show.
3. Page through `GET /session/{id}/history?after=<last seq>&limit=200` until
   `hasMore` is false. `after` is exclusive.
4. Apply the buffered stream events, skipping durable events whose `seq` you
   have already applied.

Live events missed during a disconnect are gone. Discard pending live deltas
when resynchronizing. Reopen the stream before reading snapshots, and refresh
pending permissions, questions, plans and tool calls as well as saved messages
and inputs. Tool output cursors can recover output the daemon still retains.
The next durable event or a REST read gives you the settled state.

Agent progress events keep their payload keys in snake_case, and each carries
`turnID` and `inputID`. [Writing a daemon client](daemon-client.md#event-names)
maps each event name to its payload.

## Interrupt, steer and edit the queue

Interrupt the running turn:

```bash
curl -X POST "$BASE/session/$SID/interrupt" -H "Authorization: Bearer $TOKEN"
```

```json
{"data": {"interrupted": true, "turnID": 7}}
```

When nothing is running the response is `{"interrupted": false, "turnID": null}`.
Interrupt returns once the daemon has saved the interrupted turn. The worker
thread may still be cleaning up. If you need it to be gone, for example before
changing the session's client configuration from another process, call drain:

```bash
curl -X POST "$BASE/session/$SID/drain?timeoutMs=30000" -H "Authorization: Bearer $TOKEN"
```

```json
{"data": {"drained": true, "activeWorkers": 0}}
```

Drain counts workers still running, including those of retired turns and those
waiting for an execution slot. Queued inputs that have not started are not
workers, and drain does not start them. On timeout the response is 200 with
`drained: false` and the remaining count. `timeoutMs` defaults to 30,000 and
accepts 1 to 300,000. Cancelling the drain request cancels nothing. Drain
reports a moment in time. New work can start right after it returns.

To redirect a running turn, admit a new input with `delivery: "steer"`. The
daemon interrupts the turn and starts the steering input.

Pending inputs stay editable until they start. Read the queue to get its
revision:

```bash
curl "$BASE/session/$SID/queue" -H "Authorization: Bearer $TOKEN"
```

```json
{
  "data": {
    "revision": 1,
    "pendingImages": [],
    "items": [
      {
        "id": "inp_docs_001",
        "prompt": {"text": "Run the test suite and fix failures.", "attachments": [], "continuation": false},
        "delivery": "queue",
        "paused": false,
        "createdAt": "2026-11-17T12:06:42.623534+00:00",
        "pauseCause": null
      }
    ]
  }
}
```

Then send a patch with that revision and a list of operations:

```bash
curl -X PATCH "$BASE/session/$SID/queue" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"expectedRevision":1,"operations":[{"op":"setPaused","id":"inp_docs_001","paused":true}]}'
```

The daemon applies all operations or none. If the queue changed since your
revision, it applies nothing and returns:

```json
{
  "error": {
    "code": "queue_revision_conflict",
    "message": "Queue changed since revision 1.",
    "details": {"expectedRevision": 1, "actualRevision": 2},
    "requestID": "req_34c0aba7ed9a4b0ab0e34b36d92b10ef"
  }
}
```

Reread the queue and decide again. The operations are `update`,
`setDelivery`, `setPaused`, `remove`, `moveBefore`, `moveAfter` and
`moveToStart`. A patch can also admit new inputs through `admissions`, using
the same request shape and idempotency rules as `/prompt`, so you can install
new inputs and edits atomically. `resume` works as it does on `/prompt`.

## Upload images and files

Upload each attachment, then reference its URI in the prompt:

```bash
curl -X POST "$BASE/upload?sessionID=$SID" -H "Authorization: Bearer $TOKEN" \
  -F "file=@screenshot.png;type=image/png"
```

```json
{
  "data": {
    "id": "upl_4955cf45a1099bc840a06c78",
    "uri": "yoke-upload://upl_4955cf45a1099bc840a06c78",
    "name": "screenshot.png",
    "mime": "image/png",
    "size": 48211,
    "expiresAt": "2026-11-18T12:06:42.582100+00:00"
  }
}
```

```json
{
  "id": "inp_docs_002",
  "prompt": {
    "text": "Why is the sidebar clipped?",
    "attachments": [
      {"type": "file", "uri": "yoke-upload://upl_4955cf45a1099bc840a06c78", "name": "screenshot.png", "mime": "image/png"}
    ]
  }
}
```

The rules:

- Each upload is at most 20 MiB (`413 attachment_too_large`). A prompt takes
  at most 20 attachments (`400 too_many_attachments`).
- Images must be PNG, JPEG, WebP, GIF, BMP or TIFF, and the bytes must match
  the declared type (`400 invalid_image`). Other MIME types are accepted as
  files.
- With `sessionID`, only that session can use the upload
  (`403 attachment_session_mismatch`). Without it, any session can.
- `name` and `mime` in the prompt must match the upload
  (`409 attachment_identity_conflict`).
- An upload not referenced by an admitted prompt expires after one day.
  Admission pins the upload, so queued prompts survive a daemon restart.

The daemon sends images to the model as image input. It checks that the
selected model accepts images when the turn starts, not at admission, and a
model without image input fails the turn. Other files are not read into the
prompt. The daemon saves them to a local path and adds that path to the user
message, so the agent can open them with its file tools.

## Read history, context and tool traces

| Need | Request |
| --- | --- |
| Transcript of the active branch | `GET /session/{id}/message?order=desc&limit=50`, then follow `cursor.next` |
| One message | `GET /session/{id}/message/{messageID}` |
| Durable state changes since a sequence | `GET /session/{id}/history?after=<seq>` |
| Recent model-visible context | `GET /session/{id}/context` |
| Conversation tree and branches | `GET /session/{id}/tree`, then `cursor.next` for older nodes |
| Tool calls with arguments and results | `GET /session/{id}/tool-call?order=latest` |
| One tool call in full | `GET /session/{id}/tool-call/{callID}` |
| Output of a running tool | `GET /session/{id}/tool-call/{callID}/output?afterSeq=<n>` |
| Agent runs started by tools in this session | `GET /agent-run?sessionID={id}` |

Cursors are opaque. Keep the same query parameters while you follow one. A
cursor whose anchor entry no longer exists, for example after a branch change,
returns `400 invalid_cursor_anchor`. Start again from the first page.

Interruption entries project as `type: "control"`, `kind: "control"`,
`control: "interrupted"` and `text: null`. This also applies to exact legacy
interruption notices stored as assistant entries. Clients show an `Interrupted`
marker instead of an assistant reply. Stored legacy entries stay unchanged.

`/context` is for inspection and is bounded. By default it returns the 100 most
recent active entries within 500,000 characters, and reports `totalEntries`,
`retainedEntries`, `retainedChars`, `maxChars` and `truncated`. It keeps
compaction checkpoints when they are relevant. It is not the exact provider
prompt.

`/tool-call` returns rows in transcript order. With `order=latest` it returns
the newest window, and `cursor.next` pages toward older calls. `total` counts
calls that match the filters before paging. Each call's `sequence` is its
one-based position before filtering. List rows leave `resultProjection` null.
The single-call endpoint fills it with the result as the model saw it, after
secret redaction, when the tool projects its results for the model. `result`
is the redacted canonical result.

Tool output and process output use exclusive integer cursors. Pass
`cursor.next` back as `afterSeq`. `cursor.truncatedBefore` reports output the
daemon no longer retains. Live tool output and process handles exist only while
the daemon runs. Saved tool results survive restarts.

## Answer permission requests and questions

Sessions attached with an approval mode ask before running some tools, and
`acp` attachments get a `request_user_input` tool. Both create pending requests
that live in daemon memory while a worker waits:

```text
GET  /session/{id}/permission
POST /session/{id}/permission/{requestID}/reply     {"reply":"allow"} or {"reply":"deny"}
GET  /session/{id}/question
POST /session/{id}/question/{requestID}/reply       {"answers":["Option A"]}
POST /session/{id}/question/{requestID}/reject
```

The daemon announces them with live `session.permission.requested` and
`session.question.requested` events, and their resolutions with
`session.permission.resolved` and `session.question.resolved`. Each resolution
carries `by`: `user`, `turn_stopped` or `session_closed`. Interrupting the turn
denies its pending permission and rejects its pending question with
`by: "turn_stopped"`. After a reconnect, list the pending requests again. A
daemon restart drops them.

The first reply wins. A second reply to the same request returns
`409 already_resolved`, with `phase` and the stored `resolution` in `details`,
so two clients never race to answer. The daemon keeps the last 64 consumed
requests as tombstones; after that a later reply returns
`404 permission_not_found` or `404 question_not_found`.

## Change the model, compact and fork

- `POST /session/{id}/selection` with `{"provider":"openai","model":"gpt-5.5","reasoningEffort":"high"}`
  switches the model. The session must be idle (`409 session_busy`). If the
  conversation does not fit the new model's context even after automatic
  compaction, the daemon keeps the old model and returns
  `409 model_context_too_small` with `inputTokens`, `maxInputTokens` and
  `contextWindowTokens` in `details`. If the switch works but saving the
  selection fails, the response is `507 selection_metadata_not_saved`.
- `POST /session/{id}/compact` with `{"reason":"manual"}` starts compaction and
  returns 202 with an `operationID`. Call `/wait` to know when it finishes.
- `POST /session/{id}/fork` copies the session, or the path up to
  `fromEntryID`, into a new session with a new ID. Pass `location` to fork into
  another directory. The source is never changed.
- `PATCH /session/{id}` sets `title`, `pinned` or `archived`. Archiving requires
  an idle session with no queued inputs (`409 session_has_pending_work`).
- `POST /session/{id}/title/regenerate` asks the session's model for a new
  title from the saved conversation.

`GET /provider` returns `defaultSelection`, the provider and model a new
session in that directory would use. `GET /model` returns each model's
`reasoningEfforts` and `defaultReasoningEffort`.

## Recover a moved workspace

When a session's directory disappears, execution requests fail with:

```json
{
  "error": {
    "code": "session_workspace_unavailable",
    "message": "Workspace directory no longer exists: /old/project Saved history is intact. Restore the directory or explicitly relocate with `yoke resume 20261117-120642-1d2dae --relocate /new/directory`.",
    "details": {"sessionID": "20261117-120642-1d2dae", "directory": "/old/project", "status": "missing"},
    "requestID": "req_..."
  }
}
```

Yoke never creates the missing directory and never falls back to the daemon's
working directory. A session saved without a directory reports `unconfigured`
and also needs relocation. The daemon checks the directory at admission and
again before the turn starts. If the check fails after admission, the input
goes back to the queue paused, with the same ID and attachments, and the
session's runtime state records the error in `error`. Remove the input or
unpause it after fixing the directory.

Either restore the original directory, or move the session:

```bash
curl -X POST "$BASE/session/$SID/relocate" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"directory":"/new/project","expectedDirectory":"/old/project"}'
```

The response is the session with its new `location` and `workspace`. The rules:

- The destination must already exist and be a readable directory.
- Relocating changes session metadata only. It does not move files.
- `expectedDirectory` is optional. If the saved directory no longer matches it,
  the daemon returns `409 session_workspace_conflict`.
- Relocating to the current directory is a no-op.
- The session must have no running turn, operation, live tool process or queued
  input, paused or not (`409 session_workspace_busy`). Interrupt, drain and
  remove queued inputs first.
- Relocation clears attached client configuration. Clients must attach again.

Saved branches, messages, compaction checkpoints and activated skill
instructions stay as they were. The next turn discovers configuration, tools,
skills and MCP servers in the new directory. Connected clients receive the
durable `session.workspace.relocated` event with `previousDirectory` and
`directory`.

## Daemon control

| Request | Effect |
| --- | --- |
| `GET /daemon` | pid, build, protocol, socket, port, idle exit, busy flag, pending restart and open leases. |
| `POST /daemon/restart` | Exit so the next client starts current code. Returns `409 daemon_busy` while work runs. With `{"whenIdle":true}` it schedules the exit for when work drains. |
| `POST /daemon/stop` | Exit. Returns `409 daemon_busy` while work runs. With `{"force":true}` it stops at once and interrupts running turns. |
| `GET /daemon/lease?client=<name>&pid=<pid>` | Hold a lease without receiving session events. |

The lease stream sends `daemon.lease` with `leaseID`, `pid` and `buildID`, a
heartbeat comment every 15 seconds, and `daemon.exiting` with the exit `reason`
(`idle`, `restart` or `stop`) before it closes. The CLI equivalents are
`yoke daemon status`, `yoke daemon restart [--when-idle]` and
`yoke daemon stop [--force]`.

## TypeScript client

`clients/typescript` has a small `openapi-fetch` client generated from the
OpenAPI contract. Its typed `GET`, `POST` and other methods take the same
paths as this guide, and it adds two convenience methods:

```typescript
const client = createYokeClient({ baseUrl, token });
const relocated = await client.relocateSession("my-session", {
  directory: "/new/project",
  expectedDirectory: "/old/project",
});
if (relocated.error) throw relocated.error;

const fork = await client.forkSession("my-session", {
  location: { directory: "/another/project" },
});
```

The daemon serves its OpenAPI document at `GET /openapi.json`.


## Durable input lifecycle

Protocol 3 is current. Clients require `features.inputLifecycle`, `globalEvents`,
`promptAdmission`, `steering` and `queueEditor` in `/capabilities`. They reject
a missing feature or a different protocol with an upgrade and restart message.
Each input has one durable phase: `queued`, `paused`,
`promoted`, `running`, `settled` or `removed`. Promotion reserves a turn;
`running` means the turn acquired capacity and can start its worker.

`GET /session/{id}/input?live=true` returns the live inputs, the queue hold and
`inputRevision`. Set `live=false` to include terminal inputs. Read one input
with `GET /session/{id}/input/{inputID}`. Every phase change journals
`session.input.changed` with `inputID`, `previous`, `phase`, `cause`,
`inputRevision`, `delivery`, `position`, `pauseCause`, `turnID`, `outcome`,
`clientID` and `prompt`. The revision increases within the session and survives
daemon restarts. Refresh the snapshot if you miss a revision.

Stop, failed finalization and failed model selection hold the queue with a
reason. Startup holds stranded queues with `daemon_restart`. A failed workspace
check records `workspace_unavailable`. A hold preserves the queue and lets the
daemon idle out. Resume with `POST /session/{id}/queue/resume` or an admission
or queue patch with `resume: true`. Resuming never unpauses individual inputs.

`POST /session/{id}/input/{inputID}/cancel` removes a queued or paused input.
For a promoted or running input, it stops only that input. Terminal inputs
return `409 input_settled`. The prompt admission response includes `phase` and
reads the receipt after wake, so it can already report promotion.

The daemon also holds an idle queue with reason `manual` when a request uses
`resume: false`. Hold changes publish `session.queue.held` with `held` and
`inputRevision`. Holds do not change the queue edit revision.
