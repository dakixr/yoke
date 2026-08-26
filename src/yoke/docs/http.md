# HTTP harness API

`yoke serve` starts one process-wide HTTP daemon for saved and active Yoke
sessions and serves the packaged browser application from the same origin. The
daemon owns the session runtime registry, queue admission, execution workers,
event feed, process inspector, tool inspector, and the shared services used by
the CLI. The normal interactive CLI continues to call those Python/runtime
services directly and does not route itself through HTTP.

The public API is versioned under `/api/v1`. Its OpenAPI document is available
at `/api/v1/openapi.json`. The checked-in contract used by tests is
`tests/yoke/http/golden/openapi.json`; regenerate it with:

```bash
uv run python scripts/generate_http_openapi.py
```

`clients/typescript` contains a small `openapi-fetch` client whose `paths`
types are generated from that checked-in contract. Validate contract drift and
compile the client with:

```bash
cd clients/typescript
npm ci
npm run check
```

The `HTTP contract` GitHub Actions workflow runs both the Python contract tests
and this TypeScript generation/typecheck on pushes to `main` and pull requests.

## Starting the daemon

```bash
yoke serve
```

The default host is loopback-only. `yoke serve` prints the selected port and a
bearer token. Send that token on protected requests:

```text
Authorization: Bearer <token>
```

Set `YOKE_HTTP_TOKEN` or pass the CLI token option when a stable token is
needed. Binding to a non-loopback address requires `--allow-remote` explicitly.
Do not expose the daemon to an untrusted network. It can read workspace files,
run tools and processes, and use configured model providers.

The health endpoint and packaged browser application are public. Other
`/api/v1` resources require bearer auth. Browser JSON and SSE requests use the
same bearer token printed by `yoke serve`. The web application keeps that token
in session-scoped browser storage, not long-lived local storage. A launch URL
may supply `?token=...`; the application consumes it and removes it from the
visible/history URL immediately.

The browser application is a no-build Preact + HTM application shipped under
`src/yoke/web`. `GET /`, `/new`, `/session/<id>`, and `/settings` return the
application shell, and `GET /assets/*` serves the packaged browser modules,
CSS, vendored dependencies, and licenses. These browser routes are excluded
from the v1 OpenAPI schema. Production does not require Node.js, npm, a CDN, or
a separate frontend server.

## Session and execution model

A saved session is independent of an HTTP connection. Listing sessions does
not construct model providers or live runtimes. The daemon loads a
`SessionRuntime` only when an operation needs process-local execution state.
Session listing also accepts the immediately preceding `session_stream` v1
storage format and rewrites it to the current JSONL format on first load. A
single unreadable session file is skipped rather than failing the complete
session list.

Each session has one serialized execution lane. Different sessions can execute
at the same time, subject to the daemon-wide active-session limit. Disconnecting
an HTTP request or event stream does not cancel admitted work. Use
`POST /api/v1/session/{sessionID}/interrupt` to retire the active generation.

Prompt submission uses:

```text
POST /api/v1/session/{sessionID}/prompt
```

The request accepts a caller-supplied input ID, `delivery: steer | queue`, and
`resume`. The daemon persists admission before promotion. Repeating the same ID
with the same durable admission data returns the original receipt. Reusing it
with different prompt or delivery data returns `409 input_identity_conflict`.

Pending inputs remain editable until promotion through the revision-checked
queue endpoint:

```text
GET   /api/v1/session/{sessionID}/queue
PATCH /api/v1/session/{sessionID}/queue
```

Queue patches send `expectedRevision` and an atomic operation list. A stale
revision returns `409 queue_revision_conflict` without applying any operation.

## Events and reconnect

One browser client should keep one global Server-Sent Events connection:

```text
GET /api/v1/event
```

The stream multiplexes activity from every loaded session. Completed semantic
boundaries are also written to the per-session durable event journal. Token and
tool-output deltas stay process-local and ephemeral.

Recover durable events with:

```text
GET /api/v1/session/{sessionID}/history?after=<seq>&limit=<n>
```

`after` is an exclusive per-session sequence number. The sequence can contain
gaps. A reconnecting client should refresh `/session/active`, fetch current
REST snapshots for visible sessions, then use `/history` from its last durable
sequence before treating that session as caught up.

## Main resources

The v1 API currently exposes typed resources for:

- sessions, active runtime state, messages, context, selection, compaction,
  fork, title, pin state, and durable archive state;
- prompt admission, steering, queueing, queue editing, interruption, and wait;
- session trees, navigation previews, navigation, and labels;
- tool discovery, session tool enablement, live tool traces, and sequenced
  retained tool output;
- managed process snapshots, output, stdin, interrupt, and terminate;
- providers, models, reasoning effort metadata, skills, and skill activation;
- MCP inspection and session-local MCP policy;
- contained filesystem list, find, and read operations;
- image uploads and durable prompt attachments;
- process-local permission and question requests for future human-in-the-loop
  tools and providers;
- command-palette metadata and capability discovery.

The process and raw live-output inspectors are runtime-retained. Their final
conversation results may survive restart, but PIDs and retained stdout chunks
do not.

## Session archive state

The visible browser action is called `Settle`, but the HTTP/domain field is
`archived` to avoid colliding with prompt admission's existing
`session.prompt.settled` terminology.

```text
PATCH /api/v1/session/{sessionID}
  { "archived": true | false }

GET /api/v1/session?archived=true|false
```

`SessionInfo.archivedAt` is the durable archive timestamp. Archiving is user
organization only, separate from runtime state, and reopening clears the
timestamp without changing the conversation.

## Files and uploads

Filesystem endpoints canonicalize requested paths and reject traversal or
symlink escapes outside the authorized root.

Prompt images use `POST /api/v1/upload`, then reference the returned opaque
`yoke-upload://...` URI in prompt admission. Uploads are bound to a session.
Admission pins referenced uploads so queued prompts can survive daemon restart.
The public message projection does not reveal the daemon's upload path.

## Human input

Pending process-local requests are available at:

```text
GET  /api/v1/session/{sessionID}/permission
POST /api/v1/session/{sessionID}/permission/{requestID}/reply

GET  /api/v1/session/{sessionID}/question
POST /api/v1/session/{sessionID}/question/{requestID}/reply
POST /api/v1/session/{sessionID}/question/{requestID}/reject
```

The shared human-input service exposes blocking worker-side waits. A future tool
or provider can therefore suspend on a domain request and let the browser answer
it without calling terminal input functions. Pending requests are process-local
in v1 because the worker waiting for them is process-local too.
