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
global Shift+Cmd+O shortcut on macOS, or Shift+Ctrl+O on Windows and Linux,
creates a new session from anywhere in the web UI. Cmd+B on macOS, or Ctrl+B on
Windows and Linux, toggles the sessions sidebar without stealing the browser's
Shift-modified variant. From a new-session draft, Cmd+Enter on macOS or
Ctrl+Enter elsewhere creates and starts that session in the background, then
opens a fresh draft with the same location, provider, model, and reasoning
effort instead of navigating into the running session. The
Ctrl+X chords open tools (`Ctrl+X O`), processes (`Ctrl+X P`), queue, model
selection, and the session tree. The process chord also accepts the CLI-style
`Ctrl+X Ctrl+P` form. `/shortcuts` and `?` show the browser shortcut summary.
Alt+V is terminal-specific; browser clipboard security requires Cmd+V/Ctrl+V.
Typing `/` at the start of an empty prompt opens the browser slash-command
completion menu using `/api/v1/command` metadata. The menu supports keyboard
navigation and completion; moving the active option with the keyboard keeps it
inside the menu's own scroll viewport. `/skill <name> [prompt]` and `/mcp <server>`
complete their arguments from the current workspace catalogs. Supported slash
commands execute through the corresponding HTTP operation instead of being sent
to the model as prompt text. Commands that require a saved session are disabled
while editing a new-session draft.

Each saved session has its own browser composer draft. Prompt text and pending
image attachments stay with that session while the user navigates to another
session, settings, inspectors, or other browser routes, and they are restored
when the session is opened again. Saved-session composer drafts use per-tab
session storage, so they also survive a page reload without becoming long-lived
browser data. A successful prompt admission clears that session draft; a failed
admission restores the exact text and attachments.

Provider image limits apply only to provider-bound projections. When a model
has a request-wide image limit, Yoke keeps the newest allowed images and
replaces older image parts with text notes while leaving canonical conversation
history unchanged. OpenCode-Go GLM-5.3-Flash currently uses an eight-image
request limit. A newest user message that itself exceeds the provider's
per-message limit is rejected instead of silently dropping fresh attachments.

## Session and execution model

A saved session is independent of an HTTP connection. Listing sessions does
not construct model providers or live runtimes. The daemon loads a
`SessionRuntime` only when an operation needs process-local execution state.
Session lists and recent-location discovery are served from the lightweight
session index rather than parsing conversation history. The index stores the
selection and tree summary needed by list cards, the latest user-message time,
and a file signature. Changed,
missing, legacy, or unreadable session files are repaired individually. The
session-list response includes the filtered total separately from the paged
rows, so UI counters do not expose the current page size as product state. The
browser retains that total during both initial bootstrap and later list
refreshes, keeping the settled-session shelf available immediately. The
daemon binds and can open the browser before filesystem repair and retention
maintenance. Existing stores get a short startup grace period, then repair runs
in a background task, so `yoke serve --open` and list requests never pay for
those scans. Parsed index snapshots are reused until `index.json` changes. An
index written by an older Yoke version is enriched by background maintenance;
steady-state list latency is independent of conversation-history size.
The browser requests `lastUserDesc` ordering for its session sidebar, so agent
completion, tool activity, title edits, and model changes do not move a session
ahead of one the user interacted with more recently. Sessions without a user
message fall back to their creation time. Pinning partitions that recency order
into pinned sessions followed by the normal inbox without changing relative
order inside either group. `Alt+Up` and `Alt+Down` follow that same visual order,
so keyboard navigation matches the rows shown in the sidebar.
Sidebar status uses current work before historical completion state. `Done`
means an unreviewed successfully completed turn, and a later running, stopping,
attention, error, or pending-queue state replaces it immediately. Queue counts
separate runnable steer/queued prompts from paused prompts. Session-list and
single-session refreshes keep a newer locally known queue revision instead of
letting an older summary overwrite the sidebar card. Settling is rejected while
the runtime is busy or any queued prompt remains, including paused prompts.
Sending a prompt to a settled session reopens it automatically as part of the
same prompt-admission request; the browser mirrors that reopen optimistically
and restores the settled state if admission fails.
The sidebar connection dot also follows the event-stream state rather than
remaining green during reconnect or resynchronization.
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
- working-location browsing with real directory completion, plus contained
  filesystem list, find, and read operations;
- image uploads and durable prompt attachments;
- process-local permission and question requests for future human-in-the-loop
  tools and providers;
- command-palette metadata and capability discovery.

The process and raw live-output inspectors are runtime-retained. Their final
conversation results may survive restart, but PIDs and retained stdout chunks
do not. The bundled web UI presents process inspection as a large split-pane
workspace inspired by the CLI fullscreen inspector. The left pane uses the
same compact status/session/elapsed/command rhythm as the CLI and defaults to
running processes only; a Running/All switch reveals completed and failed
retained processes without making them compete with active work. The newest
visible process is selected automatically when the current selection falls out
of the filter. The right pane is dominated by the selected process's retained
output tail. While it runs, the browser consumes `/process/{id}/output` with an
exclusive sequence cursor and appends only new chunks. Process-change events
trigger low-latency reads, with a short polling fallback so a continuously
chatty process cannot starve its own updates. Full process metadata refreshes
at a slower cadence and restores the authoritative retained tail if the output
cursor falls behind truncation. Output follows the live tail until the user
scrolls upward, then exposes an explicit "Jump to live" action instead of
stealing the scroll position. Status facts, working directory, wrapping,
stdin, interrupt, and terminate controls remain adjacent to that terminal
surface.

The new-session working-location control uses `GET /api/v1/location/browse`
instead of a browser-native datalist. The endpoint accepts an absolute path or
a path beginning with `~`, lists real child directories on the Yoke host, and
returns the exact directory when the typed path is selectable. The web picker
keeps recent session roots as a separate convenience list, supports keyboard
navigation and explicit parent-folder traversal, hides dot-directories until
the typed leaf begins with `.`, and discards stale browse responses when the
user moves to another path before an older request finishes. A draft changes
its actual working location only when the user chooses a recent root or uses
an existing browsed folder, so partial path typing does not trigger provider
catalog requests against invalid directories.

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

Untitled sessions use that same shared title generator automatically when the
first prompt starts. Title generation runs alongside the turn, so prompt
admission and model execution do not wait for it. The generator sees the saved
conversation plus the newly promoted user message and uses the session's
configured provider/model. Yoke publishes the result through the normal
`session.updated` event. If generation returns no usable title, Yoke falls back
to the first prompt text, matching the CLI behavior. A title supplied when the
session is created, or one set manually before generation finishes, is left
unchanged.

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

Provider/model and reasoning-effort changes are serialized against prompt
turns, but they are configuration mutations rather than agent activity. They do
not publish a transient `running` session state, so changing effort does not
replace the composer send controls or mark the session as Working.

Normal prompt submission transfers the draft from the composer to the
optimistic transcript before waiting for HTTP admission. The textarea is
briefly locked during that admission boundary so the same prompt cannot remain
visible in both the editor and chat. If admission fails, the browser removes
the optimistic row and restores the exact text and attachments to the composer.
Newly created sessions track message-snapshot hydration separately from local
session metadata. Returning to a session that was created optimistically but
never received an authoritative message page therefore reloads its transcript
instead of treating an empty local message array as final.
The regular composer grows with its text up to a compact height cap and offers
an explicit larger editing mode for long prompts. The expand/compact control
lives at the top-right of the composer itself; the larger mode raises only the
local editor height limit and does not alter draft or submission semantics.
Provider and model selection uses one searchable web picker rather than native
browser selects. The picker loads the model catalog across providers, groups
rows by provider, marks the current model, and shows context-window and
thinking metadata in the same choice list. Arrow keys, Home/End, and
PageUp/PageDown move the active model while keeping that row visible in the
scrolling results list; Escape closes the picker and returns focus to its
trigger. Reasoning effort remains adjacent
as compact web buttons, so the entire model control uses one consistent custom
interaction on desktop and mobile.
Switching to a smaller-context model first attempts the normal transactional
automatic compaction. If the resulting context still does not fit, the server
returns `model_context_too_small` with the estimated input size and target
context budget. The picker stays open, restores the previous selection, and
shows the failure inline instead of leaving the optimistic model change visible.
When the responsive layout collapses the session sidebar, fine-pointer devices
also expose it from the extreme left screen edge. The edge reveal is transient,
uses a short hover delay to avoid accidental activation, stays open while the
pointer is inside the sidebar, and closes after a brief leave grace period. The
hamburger button still controls the normal persistent open state, and touch-only
devices do not receive an invisible edge target.
Above the composer, opposite the model selectors, a compact context-window ring
shows the latest model-visible input usage as a percentage. Its tooltip includes
the measured input/max token counts, remaining capacity, and explains that Yoke
may compact before the model's raw limit to preserve output headroom. The latest
safe provider-reported context-usage snapshot is stored in lightweight session
metadata as soon as the model response arrives, then journaled when the turn
settles. This keeps the ring current across reconnect/reload during long tool
runs without replaying old history. Normal runtime checkpoints preserve the
measurement; changing provider/model, forking, or checking out a different tree
branch clears it rather than displaying usage from a different provider
context.

Turns that run for at least one minute and produce a persisted turn leaf keep the
same compact summary used by the CLI. The web transcript renders it after the
turn as `Worked for 1m23s · 7 tools`. Tool count follows runtime tool-start
events, so parallel calls count individually, and the summary survives reloads
and message pagination because it is stored on the persisted turn leaf.
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

Transcript pagination treats the server cursor as recoverable browser state.
If a reconnect or branch change leaves an old cursor anchor invalid, the web UI
rebases from the current newest message page and retries the older-page read.
If a rewound cursor returns only entries that are already loaded, the client
advances through duplicate-only pages until it reaches genuinely older history
or the beginning of the conversation. Pagination failures are shown as notices
instead of leaving the "Load older turns" button looking like a no-op.

While a session runtime is running, the conversation keeps a persistent status
row at the bottom of the chat. Runtime activity such as `Thinking`, `Running
tool`, or `Compacting` refines the label, but transiently missing activity data
falls back to `Working`. Live tool rows do not suppress this status, and stale
activity cannot keep it visible after the runtime returns to idle.

The transcript renders each tool call as one row. Persisted tool results are
folded into the originating call row when both are present in the loaded
window, while an orphaned result at a pagination boundary remains visible until
its call is loaded. Consecutive tool-only assistant batches are visually joined
into one dense run, so sequential batches no longer inherit full assistant-turn
spacing between them. Commentary that launches tools also stays joined to an
immediately following tool-only batch, so one continuous tool run does not gain
a blank turn gap after its introductory text. Text-only commentary and final
text keep normal separation.
Provider-visible messages injected by tools, such as the multimodal user-role
message produced by `attach_image` or `image_generation`, are persisted as
`tool_context` nodes rather than real user turns. They remain in provider/runtime
context unchanged, but normal web and CLI chat scrollback excludes them and Tree
shows them only as technical nodes. Older sessions that persisted these injected
images as `user` nodes are recognized from their tool-call ancestry on read, so
they do not reappear as false user messages after upgrade.
When one assistant response contains several tool calls, Yoke persists every
matching tool result before appending any provider-visible `tool_context`
messages. This keeps the provider tool-call batch valid for parallel image
attachments and other tools that inject follow-up context. If a previous build
or an interrupted process left an incomplete tool batch at the active leaf,
runtime resume appends explicit cancelled results for the unfinished calls before
accepting another user message. User interruption checkpoints apply the same
closure rule.
User turns are right-aligned and assistant turns remain
left-aligned, using placement rather than a separate role color. User messages
always show their role/time metadata. Within each assistant turn, only the last
assistant row containing text shows assistant role/time metadata, so intermediate
commentary and tool-calling rows do not repeat the same rail.

Web inspectors open in a large centered modal workspace instead of consuming a
right sidebar. A shared top switcher moves between Tree, tool activity,
processes, tools, skills, MCP, and session info without closing the inspector;
small screens use the same workspace fullscreen. The layout borrows the CLI
inspectors' dense pane hierarchy, status treatment, and shortcut footer while
remaining native browser UI.

Tool activity uses the same split-pane model as the CLI inspector: a dense,
searchable call list stays on the left while the selected call's detail remains
visible on the right. Calls are chronological from top to bottom. The list opens
at the bottom with the newest call selected and follows new calls while the user
stays near the tail; scrolling upward suspends that automatic following until
the user returns to the bottom. A call opened from
the chat timeline becomes the inspector's explicit selection, even when that
historical call is older than the newest retained sidebar page. Choosing another
call replaces that selection. Detail requests are race-safe, so a slower response
from an earlier chat or sidebar click cannot replace the latest selection.
Background tool refreshes follow the selected call ID rather than whichever
detail happened to finish last. Duplicate requests for the same selected call
are coalesced, and persisted calls do not request the live-output endpoint. The
HTTP tool-trace service caches reconstructed persisted traces for the most
recently inspected session, keyed by session-file revision, so moving between
historical calls reuses the same parsed trace map until the session advances or
its HEAD changes. Status, turn/iteration, duration, arguments, retained output,
result, and surrounding context are shown as one readable detail document, with
raw JSON and wrapping controls available when needed.

Arguments and results render as fields rather than JSON dumps. Scalars collapse
into a compact chip row, long strings and nested structures get their own
labelled blocks, tall payloads clamp behind an expander, and each card can be
copied as JSON. The model-sent and normalized executed arguments are merged into
a single arguments card: fields the tool filled in are tagged `default`, fields
it rewrote are tagged `adjusted` with the sent value in the tooltip, and fields
the model sent that never reached the tool are tagged `dropped`. Result payload
already shown by the output pane is not repeated in the result card.

The Tree inspector is optimized around moving the current conversation HEAD.
It defaults to user messages and final assistant messages. Mid-turn assistant
commentary, tool, control, and other technical nodes remain available behind
the `All nodes` view. Legacy assistant rows without an explicit phase are
treated as final messages. Tree API rows expose assistant `phase` so the browser
does not infer commentary from text or topology. History renders
oldest at the top and the current HEAD toward the bottom as a git-style graph
with a dedicated active lane, reusable colored branch lanes, circular nodes,
and curved fork connectors. Active-path edges remain visually dominant while
abandoned branches recede, and hidden technical nodes are bridged so
message-only mode preserves the real topology. Opening Tree anchors on HEAD,
`Jump to HEAD` returns there at any time, and loading older pages preserves the
visible history position. Opening Tree also places browser focus on HEAD so its
keyboard navigation works immediately without first clicking a row. Tree rows
use roving keyboard focus: Up/Down move
chronologically, Home/End and PageUp/PageDown cover long histories, Left moves
to the visible parent, Right moves to a visible child, and Enter/Space selects
the focused node as a checkout target. Keyboard movement keeps the focused row
inside the scroll viewport.

Clicking any non-current row selects it as a `TARGET` and opens a checkout-style
confirmation pane. The pane states how many active nodes will become abandoned,
makes clear that abandoned work is retained and can be checked out again,
shows any prompt text restored to the composer, and keeps the abandoned-path
details collapsible. An optional branch handoff note can be persisted before
the explicit `Move HEAD here` action. After checkout the preview closes and the
graph follows the new HEAD. The browser still requests bounded tree pages to
keep DOM and topology work controlled on large sessions.

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
