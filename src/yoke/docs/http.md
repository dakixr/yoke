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
yoke serve --open
```

The default host is loopback-only. `yoke serve` prints the selected port and a
bearer token. Send that token on protected requests:

```text
Authorization: Bearer <token>
```

Uvicorn lifecycle and access logs are quiet by default. Pass `--verbose` when
you want request logs plus the normal Uvicorn startup and shutdown messages.
The Yoke listening URL and bearer token are still printed in either mode.

Set `YOKE_HTTP_TOKEN` or pass the CLI token option when a stable token is
needed. Binding to a non-loopback address requires `--allow-remote` explicitly.
Do not expose the daemon to an untrusted network. It can read workspace files,
run tools and processes, and use configured model providers.

`yoke serve --open` launches the packaged UI in the default browser with the
current bearer token in a URL fragment. Fragments are not sent in HTTP requests,
so the token does not appear in access logs. The UI stores it in session-scoped
browser storage and immediately removes it from the address/history URL. When
the server binds to a wildcard address, the local browser launch uses
`127.0.0.1` rather than the wildcard host. Query-string token launch URLs remain
accepted for compatibility and are removed in the same way.

For a Tailscale-only listener, bind directly to the machine's Tailscale address
instead of `0.0.0.0`:

```bash
yoke serve --host "$(tailscale ip -4)" --allow-remote
```

This still requires bearer authentication. The host opt-in controls the bind;
network reachability remains subject to the machine's firewall and Tailscale
ACLs.

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
a separate frontend server. When image attachments are enabled, the composer
accepts images from the file picker, drag and drop, and the browser paste event
used by Cmd+V on macOS or Ctrl+V on other platforms. Composer keyboard behavior
tracks the interactive CLI: Enter sends or steers, Tab queues, Shift+Tab cycles
the model's reasoning effort, and Esc Esc interrupts. Shift+Enter, Ctrl+J, and
Esc then Enter insert a newline; Ctrl+U removes the last pending image. The
Ctrl+X chords also match the CLI for tools, processes, queue, model selection,
and the session tree. `/shortcuts` and `?` show the browser shortcut summary.
Alt+V is terminal-specific; browser clipboard security requires Cmd+V/Ctrl+V.
Typing `/` at the start of an empty prompt opens the browser slash-command
completion menu using `/api/v1/command` metadata. The menu supports keyboard
navigation and completion, and `/skill <name> [prompt]` and `/mcp <server>`
complete their arguments from the current workspace catalogs. Supported slash
commands execute through the corresponding HTTP operation instead of being sent
to the model as prompt text. Commands that require a saved session are disabled
while editing a new-session draft.

## Session and execution model

A saved session is independent of an HTTP connection. Listing sessions does
not construct model providers or live runtimes. The daemon loads a
`SessionRuntime` only when an operation needs process-local execution state.
Session lists and recent-location discovery are served from the lightweight
session index rather than parsing conversation history. The index stores the
selection and tree summary needed by list cards plus a file signature. Changed,
missing, legacy, or unreadable session files are repaired individually. The
daemon binds and can open the browser before filesystem repair and retention
maintenance. Existing stores get a short startup grace period, then repair runs
in a background task, so `yoke serve --open` and list requests never pay for
those scans. Parsed index snapshots are reused until `index.json` changes. An
index written by an older Yoke version is enriched by background maintenance;
steady-state list latency is independent of conversation-history size.
Session listing also accepts the immediately preceding `session_stream` v1
storage format and rewrites it to the current JSONL format on first load. A
single unreadable session file is skipped rather than failing the complete
session list.

Large-session reads use a separate persistent byte-offset/topology sidecar.
The initial latest-message, Context, Tree, and nearby tree-navigation reads have
bounded reverse-scan paths that do not wait for a complete topology build. A
single background worker builds or catches up the full sidecar afterward.
Normal append-only writes reconcile incrementally, so a session growing from
hundreds of megabytes does not make each refresh reread its historical prefix.
Queue, permission, question, skill, metadata, and process-local tool/runtime
lookups do not load conversation history merely to prove that a session exists.

`GET /session/{sessionID}/context` is intentionally bounded for inspection. It
defaults to 100 recent active entries and a 500,000-character text budget and
reports `totalEntries`, `retainedEntries`, `retainedChars`, `maxChars`, and
`truncated`. Compaction checkpoints are retained when relevant. The Tree
inspector likewise pages lightweight nodes, with 200 newest nodes on the first
page and an opaque cursor for older nodes. The bundled web inspector requests
80 nodes at a time to keep the sidebar DOM bounded while preserving the wider
HTTP default for API clients. Navigation previews retain at most 100
abandoned-entry previews and report the total/truncation state. These bounds are
HTTP-inspector behavior; they do not change the conversation used by the model
runtime.

Canonical full-session forks use a stable file clone plus a small metadata
delta instead of decoding and reserializing every historical message. The HTTP
path returns lightweight fork metadata and seeds the fork's read sidecar. When
the source has a persisted read sidecar, the fork reuses that immutable snapshot
with a hard link instead of serializing or copying the topology map again. The
sidecar writer uses atomic replacement, so later source-index updates do not
mutate the fork's linked snapshot. CLI callers that require an immediately
materialized `SessionRecord` keep that existing return contract.

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
  fork, title updates/regeneration, pin state, and durable archive state;
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

`/session/active` includes a process-local `activity` label while work is in
flight. It uses the same status state machine as the CLI (`Thinking`,
`Streaming`, `Running tool`, `Compacting`, provider retry states, and
`Recovering`) so browser reconnects can restore the current activity text.

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

## Session title regeneration

The browser can request a new title from the saved conversation without
changing the transcript:

```text
POST /api/v1/session/{sessionID}/title/regenerate
```

Yoke builds the title with the same shared title generator used by the CLI,
using the session's configured provider/model and the full saved conversation.
This is intentional: title generation can reuse provider cached input even for
large contexts, so the HTTP performance paths do not replace it with a lossy
conversation sample. The resulting title is then persisted through the normal
session patch/event path. The
`sessionTitleRegeneration` capability flag indicates support.

## Browser optimistic updates

The packaged web client applies mutations locally when their immediate result
is deterministic, then reconciles against the authoritative response or SSE
state. Prompt admission paints the caller-supplied input immediately, queue
edits retain pending local operations across stale snapshots, session title,
pin, archive, model/effort, tool, skill, and MCP changes are local-first, and
permission/question replies disappear immediately with rollback refreshes on
failure. New-session submission switches into the created session and paints
the prompt before admission completes. Compaction shows its local activity
state immediately. Tree labels serialize revision-checked mutations while
keeping the newest local label visible.

Normal prompt submission transfers the draft from the composer to the
optimistic transcript before waiting for HTTP admission. The textarea is
briefly locked during that admission boundary so the same prompt cannot remain
visible in both the editor and chat. If admission fails, the browser removes
the optimistic row and restores the exact text and attachments to the composer.
The regular composer grows with its text up to a compact height cap and offers
an explicit larger editing mode for long prompts; the larger mode raises only
the local editor height limit and does not alter draft or submission semantics.
The daemon warms the lazy HTTP runtime import in the background after first
paint, and cold first-turn reconstruction gives the admission response a short
grace before a large saved session begins CPU-heavy parsing. During that cold
state the runtime reports `Loading session`, then changes to `Thinking` after
the saved model state is ready. SessionRuntime is also the sole owner of loading
active conversation state; the HTTP agent factory no longer restores the same
history a second time before provider work can begin.

When a current persistent topology sidecar contains a self-contained compaction
checkpoint, cold turn startup reconstructs the compact runtime state directly
from indexed entry offsets instead of deserializing the historical session
prefix. The reconstructed provider-message projection and conversation cache
scope are required to match the established full-tree projection. Prompt startup
does not synchronously build a missing topology sidecar: a cold sidecar miss
falls back to the normal session read rather than scanning the same large file
twice, while transcript and Tree tail reads continue to warm sidecars in the
background. A stale append-only sidecar is caught up incrementally.

Turn checkpoints and settlement persist only the suffix created by that turn,
grafted onto the canonical saved leaf, so the reduced runtime representation
never rewrites historical parent links. Full-history startup owns mutable entry
shells while borrowing historical message values; provider-bound normalization
still creates defensive message copies. Compaction fast paths are guarded by
differential tests against the established provider projection, including tool
turns, images, skills, prior checkpoints, branches, response continuity, and
conversation cache identity.

Operations that must wait for an authoritative response expose pending state
instead of pretending the result already happened. The browser shows compact
spinners for transcript and Tree pagination, Tree navigation previews and
checkout, manual compaction, and other blocking composer/inspector work while
preventing duplicate submissions.

While a session runtime is running, the conversation keeps a persistent status
row at the bottom of the chat. Runtime activity such as `Thinking`, `Running
tool`, or `Compacting` refines the label, but transiently missing activity data
falls back to `Working`. Live tool rows do not suppress this status, and stale
activity cannot keep it visible after the runtime returns to idle.

The transcript renders each tool call as one row. Persisted tool results are
folded into the originating call row when both are present in the loaded
window, while an orphaned result at a pagination boundary remains visible until
its call is loaded. User turns are right-aligned and assistant turns remain
left-aligned, using placement rather than a separate role color. User messages
always show their role/time metadata. Within each assistant turn, only the last
assistant row containing text shows assistant role/time metadata, so intermediate
commentary and tool-calling rows do not repeat the same rail. The Tree
inspector defaults to user and assistant message nodes only. Tool, control, and
other technical nodes stay available behind a "Show all nodes" toggle. The
inspector uses a compact connected-node layout and keeps its browser page size
bounded to reduce DOM work on large sessions.

Checkpointed tool-calling assistant messages also reconcile with their live
mid-turn commentary row. Providers may persist that message with a null phase
even though Yoke emits its text live as `commentary`; the browser treats text
plus tool calls as the same commentary message. This removes the live tail row
as soon as the checkpointed message arrives instead of leaving a duplicate at
the bottom until the turn settles.

Session bootstrap also separates core data from enrichment. Session lists and
the selected transcript can become interactive without waiting for provider
readiness probes or Git/location metadata. Queue and human-input snapshots load
in parallel after the transcript rather than blocking first paint. Optimistic
generations and mutation chains prevent older HTTP responses or durable events
from visually undoing a newer click or resurrecting an already durable prompt.

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
