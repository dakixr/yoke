# Yoke web UI

## Status

Proposed. This plan defines the browser UI, its no-build frontend runtime, and
the small HTTP-contract additions needed to support it.

The existing CLI architecture does not change. The CLI remains a direct Python
client of Yoke runtime and session code. The HTTP API exists for the browser UI
and is not a replacement for the CLI's direct path.

## Summary

Build a local-first web UI for managing several Yoke coding-agent sessions at
once. The product model should be closer to T3Code than to an IDE or a normal
chat application.

The default desktop UI is a persistent session sidebar plus the active
conversation. A contextual right inspector opens only when the user asks for
tree, tool, process, or diff detail.

The sidebar is the primary work manager. It should make it obvious which
sessions need human input, which are still working, which finished while the
user was elsewhere, and which old sessions have been settled into history.

The frontend has no Node.js dependency and no frontend build step. Ship native
browser JavaScript and CSS inside the Python package and serve it from the same
FastAPI process as `/api/v1/*`.

Use:

```text
Preact
HTM
native ES modules
vanilla CSS
vendored browser dependencies
FastAPI static serving
JSON HTTP API
one global authenticated SSE stream
```

The files committed to Git are the files the browser executes.

## Product goals

The UI should let one user operate several long-running Yoke sessions without
opening each one to discover its state.

The core questions are:

```text
What sessions exist?
Which ones need me?
Which ones are still working?
What finished while I was elsewhere?
How do I switch to the right one immediately?
```

The first useful release should support the complete loop:

```text
create session
send prompt
leave it working
switch to other sessions
notice completion or human-input requests
return to the session
steer or queue more work
inspect tools/process/tree when needed
settle the finished session
```

## Non-goals

The first version is not an IDE. It does not need editor tabs, a permanent file
explorer, arbitrary split panes, a terminal grid, or a VS Code-like workbench.

It also does not need Node.js, npm, pnpm, Bun, Vite, Webpack, Rollup,
TypeScript compilation, Tailwind, server-rendered HTML fragments, top session
tabs, multiple Yoke server connections in one window, snoozed sessions,
full-text message search, or PTY terminal emulation.

These can be added later without changing the basic layout.

## Architectural boundary

Keep the current boundary explicit:

```text
                         Yoke domain/runtime
                                │
                   ┌────────────┴────────────┐
                   │                         │
                   ▼                         ▼
               CLI / SDK                 HTTP layer
             direct Python              projection and
                  calls                 remote commands
                                             │
                                             ▼
                                          Web UI
```

The browser consumes a stable HTTP projection. It must not depend on internal
Python object shapes or force the CLI onto HTTP for consistency.

Route handlers should remain thin. Browser-specific orchestration belongs in
HTTP services/projectors, while session and runtime behavior stays in the same
core code used by the CLI.

The HTTP side is already a substantial browser contract, not a thin prototype.
It has a checked-in OpenAPI 3.1 snapshot under
`tests/yoke/http/golden/openapi.json`, contract tests that compare FastAPI's
generated schema to that snapshot, and a typed reference client under
`clients/typescript`. Treat those artifacts as the source of truth for route
names, request/response shapes, camelCase aliases, errors, and pagination.

The browser UI does not need to consume the TypeScript package. Its existence
does not change the no-build browser requirement. The browser can use a small
native-JavaScript adapter while the checked-in OpenAPI and TypeScript client
continue to catch HTTP contract drift for other consumers.

## Information architecture

### Default desktop layout

Use two persistent columns:

```text
┌──────────────────────┬───────────────────────────────────────────────┐
│ Session sidebar      │ Active session                                │
│                      │                                               │
│ drafts               │ conversation                                  │
│ pinned               │ tool/activity summaries                       │
│ project groups       │ pending human input                           │
│ settled              │ queue                                         │
│                      │ composer                                      │
└──────────────────────┴───────────────────────────────────────────────┘
```

Do not reserve permanent width for an inspector.

### Contextual inspector

Open a third column only when the user asks for detail:

```text
┌──────────────────┬──────────────────────────────┬────────────────────┐
│ Sessions         │ Conversation                 │ Inspector          │
│                  │                              │                    │
│                  │                              │ Tree               │
│                  │                              │ Tool               │
│                  │                              │ Process            │
│                  │                              │ Diff               │
└──────────────────┴──────────────────────────────┴────────────────────┘
```

Closing the inspector returns its width to the conversation.

### No top session tabs

Do not add session tabs across the top. The sidebar, search, pinned sessions,
and keyboard navigation should be fast enough to handle switching.

This keeps one navigation model instead of maintaining both sidebar state and
tab state.

## Frontend runtime

Use Preact with HTM.

Preact gives the UI component lifecycle, hooks, keyed rendering, and fine
enough update boundaries without requiring the normal React build toolchain.
HTM gives JSX-like templates as ordinary tagged JavaScript template literals.

Example:

```js
import { html, render, useState } from "./vendor/htm-preact.js";

function SessionRow({ session }) {
  return html`
    <button class="session-row" data-selected=${session.selected}>
      <span class="session-row__title">${session.title}</span>
      <span class="session-row__status">${session.status.label}</span>
    </button>
  `;
}
```

Application modules use native browser imports. Do not bundle them.

Use JavaScript with `// @ts-check` and JSDoc for transport/store types that
benefit from editor checking. Do not add TypeScript compilation solely for the
web UI.

## Browser dependencies

Production must not depend on a public CDN.

Vendor only browser-ready distributable files that the application actually
loads. The initial set should be approximately:

```text
Preact + hooks + HTM integration
Markdown parser
HTML sanitizer
```

Syntax highlighting can wait until plain fenced code blocks work well. If it
is added later, vendor a browser-ready highlighter rather than introducing a
build tool.

Each vendored dependency should record an exact version and preserve its
license. Do not vendor an npm dependency tree.

## Proposed package layout

Keep the browser app inside the Python package:

```text
src/yoke/web/
  index.html

  assets/
    css/
      reset.css
      tokens.css
      layout.css
      sidebar.css
      timeline.css
      composer.css
      inspector.css
      dialogs.css
      responsive.css

    js/
      main.js
      app.js

      api/
        client.js
        errors.js
        events.js
        sse.js

      state/
        store.js
        bootstrap.js
        reducer.js
        session-cache.js
        local-state.js

      router/
        router.js
        routes.js

      components/
        app-shell.js
        sidebar.js
        session-row.js
        session-group.js
        status-label.js
        modal.js
        menu.js

      session/
        session-view.js
        header.js
        timeline.js
        turn.js
        message.js
        markdown.js
        tool-summary.js
        queue.js
        composer.js
        human-input.js

      inspector/
        inspector.js
        tree.js
        tool.js
        process.js
        diff.js

      lib/
        duration.js
        keyboard.js
        focus.js
        autoscroll.js
        collections.js

    vendor/
      htm-preact.js
      markdown.js
      sanitizer.js
```

Create files as behavior appears. The important ownership boundaries are API,
sync/store, sidebar, session timeline/composer, and inspector. Avoid one large
`app.js` that mixes all of them.

## Static serving and routing

Serve the browser application from the same FastAPI process as the API.

Use paths similar to:

```text
GET /                         application shell
GET /session/<id>             application shell
GET /settings                 application shell
GET /assets/...               packaged static files

/api/v1/...                   existing HTTP API
```

Use a small History API router in the browser. FastAPI should return
`index.html` for known application routes while leaving `/api/v1/*` and
`/assets/*` untouched.

Add browser/static routes after the API routers and keep them out of the API
schema with `include_in_schema=False` where applicable. The existing
`/api/v1/openapi.json` contract should remain an API contract rather than start
describing HTML shell routes.

During development, return `Cache-Control: no-store` for application files so
the development loop is simply:

```text
edit JS/CSS
refresh browser
```

No separate frontend development server is required.

The Python wheel must contain every required web asset and vendored license.

Use the existing `yoke serve` daemon as the web host. Do not add a second web
server command merely to host the browser. `yoke serve` can keep its current
API role and also answer `/`, browser routes, and `/assets/*`. This does not
change the normal interactive CLI path, which continues to call Yoke directly
in Python.

## CSS

Use vanilla CSS with semantic classes and shared variables. Do not use
Tailwind.

Keep global values in `tokens.css`:

```css
:root {
  --sidebar-width: 288px;
  --inspector-width: 420px;
  --content-max-width: 920px;

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;

  --radius-sm: 5px;
  --radius-md: 8px;
  --radius-lg: 12px;
}
```

Use semantic classes:

```css
.session-row {}
.session-row__title {}
.session-row__meta {}
.session-row__status {}
.session-row[data-selected="true"] {}
.session-row[data-attention="true"] {}
```

The visual direction should be dense, quiet, and utilitarian. The sidebar may
show many sessions, so hierarchy should come from typography, spacing, subtle
backgrounds, and clear status labels rather than card chrome around every
object.

Avoid continuous decorative animation. Several running agents should not turn
the sidebar into a wall of spinners.

## Application state

Treat the browser as a live replica of the HTTP projection, not as the owner of
Yoke session truth.

Maintain one application store outside the Preact component tree. Components
subscribe to the pieces they render.

Suggested shape:

```js
state = {
  connection: {
    status,
    serverInstanceID,
    lastError,
  },

  capabilities: {},

  sessions: Map<sessionID, SessionSummary>,
  activeRuntimes: Map<sessionID, ActiveRuntimeInfo>,

  sessionData: Map<sessionID, {
    messages,
    messageCursor,
    queue,
    tree,
    permissions,
    questions,
    loaded,
  }>,

  ui: {
    selectedSessionID,
    inspector,
    filters,
  },
};
```

Keep network processing outside random UI components:

```text
HTTP/SSE
   ↓
API and event adapters
   ↓
deterministic reducer
   ↓
application store
   ↓
Preact components
```

Components may issue commands. They should not each invent their own response
to global events.

## Server truth and browser-local truth

Server truth includes session identity, title, pinned state, working directory,
provider/model selection, messages, tree, queue, runtime state, permissions,
questions, tool traces, processes, and archive state once added.

Browser-local truth includes sidebar width/collapse, collapsed project groups,
filters, inspector state, unsent drafts, last selected session, and per-browser
last-seen markers used to derive unread completion state.

Never store the auth token in persistent `localStorage`.

## Bootstrap and synchronization

Use one global event connection for the server. Do not create one SSE stream
per open or visible session.

Startup order:

```text
1. GET /api/v1/health
2. GET /api/v1/capabilities
3. open /api/v1/event
4. receive server.connected
5. buffer incoming events
6. GET initial session list
7. GET /api/v1/session/active
8. GET shell data such as recent locations/catalogs
9. install snapshots
10. reduce buffered events over snapshots
11. mark UI ready
```

Opening the stream before loading snapshots closes the race where state changes
between an initial GET and the stream connection.

Store `serverInstanceID` from `server.connected`.

On disconnect, keep loaded UI visible, show a small connection indicator,
disable commands whose result cannot be trusted, reconnect with bounded
backoff, then resynchronize before declaring state current.

The current stream has durable per-session sequence numbers but no resumable
server-wide cursor. Until that changes, reconnect should favor correctness.
Track the highest observed `durable.seq` separately for each loaded session.
Sequence numbers may contain gaps, so treat them as cursors rather than counts.

After an uncertain reconnect:

```text
1. reconnect the global stream and buffer new events
2. receive server.connected and compare serverInstanceID
3. refresh the session list so sessions created while disconnected appear
4. refresh /api/v1/session/active
5. for each loaded session, fetch /history?after=<last durable seq> until caught up
6. refresh authoritative REST snapshots for the selected/visible session
7. reduce buffered live events and mark the connection current
```

The history endpoint repairs durable semantic boundaries. It cannot replay
ephemeral streaming, process, permission, question, tool-config, MCP-config,
or active-state events. Refresh the corresponding REST snapshot when that
state matters after reconnect. If `serverInstanceID` changed, assume all
process-local state was recreated and refresh it rather than trying to preserve
old runtime/process identifiers.

Treat `server.resyncRequired` as an explicit request to run the same repair
path.

## Event reduction

Make the reducer deterministic and test it with ordered event sequences.

It needs to understand at least:

```text
server.connected
server.resyncRequired

session.created
session.updated
session.prompt.admitted
session.prompt.edited
session.prompt.removed
session.prompt.promoted
session.prompt.settled
session.queue.updated
session.interrupted
session.active.changed
session.message.updated
session.runtime.failed
session.tool.started
session.tool.ended
session.compaction.started
session.compaction.delta
session.compaction.ended
session.compaction.failed
session.context.updated
session.process.updated
session.selection.changed
session.skill.activated
session.tree.updated
session.tool.config.changed
session.mcp.updated

session.permission.requested
session.permission.resolved
session.question.requested
session.question.resolved
```

Unknown event types should be ignored safely and logged in development rather
than crashing the application.

The same public event name may appear as an ephemeral live update and later as
a durable semantic boundary. For example, `session.message.updated` can carry
live assistant progress and a durable turn-completion marker. Reducers must use
the envelope's `durable` field and payload shape rather than assuming one event
name has one retention class.

For high-frequency output, buffer events and flush UI state at most once per
animation frame. Coalesce adjacent updates that target the same message part or
progress record.

A streaming token must not rerender unrelated session rows.

## Event contract follow-up

The current public event transport is intentionally loose:

```text
type: string
data: dict[str, object]
```

Before the browser accumulates many unsafe assumptions, move the public event
contract toward event-specific Pydantic payloads with a discriminated event
type.

This is still an HTTP projection, not exposure of internal runtime event
classes.

Do this early enough that the reducer can rely on a finite public event
vocabulary, but do not block the static shell and first sidebar prototype on
it.

## Session sidebar

The sidebar is the main navigation and work-management component.

Recommended order:

```text
Yoke

+ New session
Search
Project filter

Drafts, when present
Pinned sessions
Project groups containing active sessions
Settled history

Settings
```

### Session row

Keep active rows compact:

```text
Fix HTTP reconnect semantics
yoke                         Working 2m14s
```

Useful row information is title, compact location, attention/runtime label,
working duration, optional queue count, pin affordance, and unread completion
state.

Do not load conversation history to render or hover a row.

The existing `SessionInfo` already contains title, pinned, location,
timestamps, model selection, tree summary, and queue summary. Combine it with
`/session/active` for runtime state.

### Attention priority

Human action should outrank agent activity.

Suggested display precedence:

```text
Approval     pending permission request
Input        pending question or other human-input request
Failed       runtime failed and the result is unseen
Done         previously working session became idle and the result is unseen
Stopping     runtime is stopping
Working      runtime is running
Queued 3     idle session has queued work
             no label when idle and already reviewed
```

If the browser knows only that a runtime is `waiting_input`, use `Input` as the
generic label until a permission/question snapshot or event identifies the
request more precisely.

Do not continuously animate `Working`.

### Working duration

Use `ActiveRuntimeInfo.startedAt` to show values such as:

```text
Working 18s
Working 3m12s
Working 1h04m
```

Isolate the timer component so one-second updates do not rerender the entire
sidebar.

### Unread completion

Runtime `idle` and user-level `Done` are different concepts.

`Done` means the session produced meaningful new work after the user last
reviewed it.

Keep a per-browser last-seen activity marker. When a running session becomes
idle, mark it unread if its latest meaningful activity is newer than that
marker. Clear the unread state when the user opens the session and reaches the
newest content.

Metadata changes such as rename or pin should not create unread state.

### Drafts

Do not create a server session merely because the user clicked `New session`.

If the user types meaningful content and switches away, persist the draft in
browser-local state and show it above pinned sessions:

```text
Draft
yoke
"Investigate why the event stream..."
```

Clicking it restores location, model selection, prompt text, and any locally
restorable attachments.

Discard is explicit.

### Pinned sessions

Pinned sessions appear above project groups and remain visible regardless of
location.

The server already persists `SessionInfo.pinned` and accepts pin changes in
`PATCH /session/{id}`. Use it directly.

Do not show the same pinned session again inside its project group at the same
time.

Initially sort pins by recent activity. Manual pin ordering can be added later
with a dedicated persisted ordering field.

### Project grouping

Use session working directory as the initial group identity.

For example:

```text
/home/user/dev/yoke   → yoke
/home/user/dev/site   → site
```

Preserve the full path in tooltips, menus, and ambiguous search results.

Do not add a separate project database solely for the UI. Repository-aware
grouping can come later if Yoke gains explicit repository/worktree metadata.

Project groups may be collapsed locally. A project filter should offer all
locations or one selected location.

Filtering only changes visibility. It never changes which sessions the browser
believes exist.

### Settled sessions

Add a user-level lifecycle separate from runtime state:

```text
idle       runtime is not executing
archived   user considers the session inactive/history
```

An idle session may remain active work for days. An archived session may later
be reopened and continued.

Use `archived` in the HTTP/session model even if the visible section is labeled
`Settled`. Yoke already uses `settled` for prompt-admission completion and emits
`session.prompt.settled`; reusing the same word for session organization would
make event and model semantics unnecessarily ambiguous.

Persist archive state on the server.

Suggested contract addition:

```text
SessionInfo.archivedAt: string | null

PATCH /api/v1/session/{id}
  archived: true | false

GET /api/v1/session
  archived: true | false | omitted
```

Use a timestamp so the UI also knows when the session was archived.

Normal project groups exclude archived sessions. The visible `Settled` section
shows a small first page and loads older history using the existing cursor
model.

Use the visible `Settle` action as the normal cleanup action, backed by the
server's archived state. Permanent deletion should not be the default way to
manage sidebar noise.

Defer snoozing until there is evidence that pin plus settle is insufficient.

## Search and switching

Put search directly under `New session` or expose the same input through a
command switcher.

Version one searches fields already supported by the session API, primarily
title and directory. Session ID may be included as a secondary match.

Use server search for the canonical result set. Local filtering may make
already loaded rows react instantly, but it must not imply that one loaded page
is the whole history.

The server also exposes `/api/v1/command`. Use its `name`, `description`,
`usage`, and semantic `action` values to populate slash-command and command
palette discovery rather than duplicating command descriptions in JavaScript.
The browser still owns the mapping from those semantic actions to UI flows.
For example, `session.tree` opens the tree inspector and `upload.create` opens
the attachment picker.

Full-text search over user messages and final answers can be a later indexed
server feature.

Initial keyboard commands:

```text
Cmd/Ctrl+K       session search / command switcher
Cmd/Ctrl+N       new session
Cmd/Ctrl+B       toggle sidebar
Escape           close menu, modal, or inspector
```

Previous/next and numbered session jumps can come after sidebar ordering is
stable.

## Main session view

The vertical layout is:

```text
session header
scrolling timeline
pending human input and queue when present
composer
```

The timeline gets the flexible height. The composer remains reachable without
scrolling the page shell.

Use a readable maximum width for ordinary conversation while allowing tool and
diff summaries more horizontal room when useful.

### Session header

Keep the header compact. It should show title, location, runtime state,
provider/model summary, pin action, session menu, and inspector toggle when
relevant.

Put rename, settle/reopen, fork, compact, and other lower-frequency operations
in the session menu rather than permanent toolbar buttons.

Resolve the selected session's location through `/api/v1/location` when the
header needs its human-readable name, Git root, or branch. Do not resolve every
sidebar row individually. Sidebar grouping can use the session's directory and
cache one resolved location per unique directory when richer labels are useful.

## Timeline

Render work as turns, not alternating speech bubbles.

The current HTTP projection already distinguishes:

```text
user
assistant
tool
control
```

Assistant messages also expose `commentary` and `final_answer`. Use those
semantics directly.

A typical turn should read roughly as:

```text
User request

Assistant commentary
  tool activity
  file activity
  process activity

Assistant final answer
```

Do not reconstruct provider-private reasoning in the browser. The public HTTP
projection remains the privacy boundary.

### Markdown

Render text as Markdown where appropriate. Sanitize all generated HTML before
inserting it into the DOM. The Markdown parser is not a security boundary.

Batch streaming text so the UI does not reparse a large transcript on every
token. Render the complete stable Markdown representation after the message
settles.

Plain fenced code blocks are enough for the first release. Syntax highlighting
is optional.

### Autoscroll

Follow new output only while the user is near the bottom.

If the user scrolls upward, stop forcing the viewport down and show `Jump to
latest`. Preserve visual position when expandable activity cards change
height.

When practical, restore each recently opened session's last scroll position on
switching back.

## Tool activity

The main timeline should summarize tools instead of dumping full trace payloads.

Examples:

```text
Read 6 files

Changed 3 files
  src/yoke/http/event.py
  src/yoke/session/store.py
  tests/yoke/http/test_event.py

Ran tests
  15 passed in 2.3s
```

Provide a generic renderer from the beginning so unknown tools are still
understandable. Specialized renderers can improve common tools later.

Clicking a summary opens the tool inspector.

Large tool output should load on demand. Do not copy megabyte-scale output
into every timeline render.

## Changed files and diffs

Show changed-file summaries inline when the server can identify them reliably.
Clicking a file opens the inspector in diff mode.

The first diff viewer can be a clear textual unified or split diff. It does not
need an editor runtime.

If the current API cannot provide a stable session diff, add a small
browser-oriented projection instead of reconstructing Git state in JavaScript.

## Composer

The composer must expose Yoke's steering and queue semantics.

Idle session:

```text
┌──────────────────────────────────────────────┐
│ Ask Yoke...                                  │
│                                              │
│ model · effort                         Send  │
└──────────────────────────────────────────────┘
```

Running session:

```text
┌──────────────────────────────────────────────┐
│ Add a follow-up...                           │
│                                              │
│ model · effort       [Steer now ▾]    Send   │
└──────────────────────────────────────────────┘
```

The delivery selector should make both choices explicit:

```text
Steer now
Queue next
```

Remember the last running-session delivery choice for that session, but never
silently turn an idle send into a queued prompt.

Show attachments before submission and enforce limits from `/capabilities`.

### Optimistic admission

After the prompt request is accepted but before all related events arrive, the
UI may show an optimistic local user entry or queue row keyed by the admission
ID.

Reconcile it with authoritative events/snapshots. On failure, remove the
optimistic state and restore the prompt with the API error available.

Do not fabricate optimistic assistant output.

## Queue editor

Show the queue directly above the composer whenever it contains items.

Use the existing server revision and patch operations for edits and ordering.

Example:

```text
Next up

1. Check the reconnect test too                Queue
2. Update the HTTP docs                        Queue
3. Mention the compatibility issue             Paused
```

Support the operations already present in the HTTP model: edit, change
delivery, pause/resume, remove, move before, move after, and move to start.

Optimistic reorder is acceptable only while the expected revision matches. On
conflict, reload the queue and show a compact conflict message rather than
attempting an invisible merge.

## Permissions and questions

Pending human input should be prominent without taking over the whole app.

For a permission:

```text
Permission requested
Run command outside the workspace

[Deny] [Allow]
```

For a question:

```text
Yoke needs input
Which deployment target should I use?

( ) staging
( ) production

[Answer]
```

Show `Approval` or `Input` on the sidebar row so the request is visible while
the user is in another session.

The current pending human-input state is process-local. After reconnect, reload
pending permissions/questions for sessions reported as waiting for input.

## New-session flow

`New session` opens an empty main view, not a modal.

The user chooses or confirms location, provider/model, reasoning effort where
supported, initial prompt, and attachments.

Use `/location/recent` for recent choices and `/location` for path resolution.

Do not create the server session until the first prompt is submitted:

```text
resolve location
create session
submit prompt
navigate to /session/<id>
```

If creation succeeds and prompt admission fails, keep the new server session
selected and restore the prompt for retry.

## Contextual inspector

Use one right-side inspector with modes:

```text
Tree
Tool
Process
Diff
```

The inspector may later gain MCP and context-usage modes.

Opening a tool or diff changes inspector mode without changing the active
session. Inspector width and last mode are browser-local state.

## Session tree

Treat Yoke's tree as a first-class feature.

The tree view should show current leaf, active path, abandoned branches,
labels, previews, child counts, and the current mutation revision.

Example:

```text
● Initial task
│
├─● Try approach A
│  └─○ abandoned result
│
└─● Try approach B
   └─● current
```

Selecting a non-current node first calls the existing navigation-preview
endpoint. Show abandoned-entry effects and restored editor text before
mutation.

Navigation must include the expected revision. On `tree_revision_conflict`,
reload and require the user to choose again. Do not silently retry a branch
mutation after the tree has changed.

## Tool inspector

The tool inspector is the full public detail view for one tool call. It should
support tool name, call ID, status, timing, input, output, error, trace events,
and related process where available.

Use server-redacted values only. Browser-side filtering is not the security
boundary.

Load large output on demand through the existing trace/output APIs.

## Process inspector

Show process state, bounded output, and supported control actions such as
interrupt, terminate, and stdin.

This is not a terminal emulator in the first release.

If PTY support arrives later, use a separate bidirectional raw-byte transport,
such as WebSocket, rather than mixing terminal bytes into semantic SSE events.

## Capability gating

Read `/api/v1/capabilities` and gate UI behavior from capability flags, never
from server-version comparisons.

Examples:

```text
session_tree       Tree inspector
tool_inspector     tool detail
process_inspector  process detail
queue_editor       queue mutation controls
steering           Steer now
permissions        permission cards
questions          question cards
mcp                MCP inspection and policy controls
skills             skill discovery and activation
images             image attachments/rendering
pty                terminal UI only when true
```

The application shell should remain usable when optional features are absent.

For the current v1 server, permissions, questions, MCP, skills, and images are
real HTTP features rather than placeholders. They belong in the implementation
plan, gated by capabilities so older or reduced servers can still work.

## Authentication

Use the current bearer-authenticated API first.

Normal JSON requests use `fetch` with an `Authorization` header.

Do not use native `EventSource` for the authenticated event stream because it
cannot set an arbitrary bearer header. Use `fetch` plus a small SSE parser over
the response body.

Keep the bearer token in memory or `sessionStorage`, never persistent
`localStorage`.

If `yoke serve` prints or opens an authenticated browser URL, put the bootstrap
token in the URL fragment rather than the query string. Fragments are not sent
in HTTP requests. The UI should consume it immediately, copy it to
`sessionStorage` or memory, and replace the URL so it does not remain in normal
navigation history.

A later improvement can exchange a one-time bootstrap token for an HttpOnly,
SameSite cookie. That is preferable for remote deployments but is not required
to prove the initial local UI.

## Browser API client

Do not create a second broad general-purpose JavaScript Yoke SDK inside the web
app. `clients/typescript` already fills the typed external-client role and is
checked against the golden OpenAPI contract.

Keep a thin web-specific client around the HTTP routes. It should centralize
auth, JSON parsing, request IDs, cancellation, and public error envelopes.

The rough interface can look like:

```js
api.health()
api.capabilities()

api.sessions.list(options)
api.sessions.get(id)
api.sessions.create(request)
api.sessions.patch(id, request)
api.sessions.messages(id, options)
api.sessions.tree(id)
api.sessions.previewNavigation(id, targetID)
api.sessions.navigate(id, request)
api.sessions.history(id, options)
api.sessions.selectModel(id, request)
api.sessions.compact(id)

api.prompts.submit(id, request)
api.prompts.queue(id)
api.prompts.patchQueue(id, request)
api.prompts.interrupt(id)

api.permissions.list(id)
api.permissions.reply(id, requestID, response)
api.questions.list(id)
api.questions.reply(id, requestID, response)
api.questions.reject(id, requestID)

api.catalog.providers(options)
api.catalog.models(options)
api.commands.list()
api.locations.resolve(directory)
api.locations.recent()
api.skills.list(options)
api.skills.session(id)
api.skills.activate(id, name, request)
api.tools.list(options)
api.tools.patch(id, request)
api.mcp.list(options)
api.mcp.session(id, options)
api.mcp.patch(id, server, request)
api.processes.list(options)
api.processes.output(id, options)
api.toolCalls.list(sessionID, options)
api.toolCalls.output(sessionID, callID, options)
api.files.list(options)
api.files.find(options)
api.uploads.create(file, options)

api.events.connect()
```

Preserve public error codes and request IDs so components can handle expected
conflicts such as queue and tree revision errors.

The web runtime and web development loop must not depend on Node-based code
generation. Keep the browser adapter as checked-in native JavaScript and use
JSDoc for important response/event shapes.

The repository's existing TypeScript client remains a separate contract
consumer. When the HTTP contract changes, continue regenerating the golden
OpenAPI snapshot and `clients/typescript/src/schema.d.ts` through the existing
workflow. The browser implementation should follow the same operation IDs and
public field names, but it does not import TypeScript or run npm before use.

## Session cache and pagination

The sidebar holds session summaries only.

Opening a session loads the newest message page. Older pages load when the user
scrolls toward the beginning.

Keep a bounded in-memory cache for a small number of recently opened sessions
so switching back is immediate. Drop large tool/process output more
aggressively than message summaries.

Never prefetch complete histories on hover.

Use the existing cursor pagination for sessions and messages. Preserve scroll
anchor when prepending older messages.

## Performance targets

The UI should remain usable with:

```text
200+ session summaries
10+ simultaneously active sessions
thousands of messages in a long session
large tool traces that are not expanded
streaming output arriving many times per second
```

Do not add timeline virtualization before measurement shows it is needed. If
long sessions become expensive, virtualize stable historical turns first and
keep the active streaming tail normally rendered.

Build repeatable synthetic browser fixtures before performance tuning.

## Responsive behavior

Desktop:

```text
persistent resizable sidebar
full conversation
optional right inspector
```

Tablet:

```text
collapsible sidebar
conversation
inspector overlays or replaces part of conversation
```

Mobile:

```text
full-height session drawer
session selection closes drawer
inspector becomes full-screen secondary view
composer stays anchored to bottom
```

Selecting a session on mobile must close the drawer immediately.

## Accessibility

All primary actions must work without a mouse.

Use semantic buttons, inputs, headings, lists, dialogs, and labels. Expose
selected, expanded, unread, and status state without relying only on color.

Dialogs should trap focus and restore it on close. Respect
`prefers-reduced-motion`.

Do not announce every streaming token to screen readers. Announce coarse state
changes such as completion, failure, permission request, or question request.

## Browser-local persistence

Use one versioned local-state module rather than scattered storage calls.

Persist candidates such as sidebar width/collapse, collapsed project groups,
project filter, inspector width/mode, unsent drafts, per-session last-seen
activity, and last selected session.

Namespace state by server identity where needed so two Yoke servers cannot mix
browser presentation state.

The local-state module must tolerate schema upgrades and corrupt values by
falling back to defaults.

## Error handling

Show normal failures close to the action that failed.

Examples include prompt failure near the composer, queue revision conflict in
the queue editor, tree conflict in the tree view, permission reply failure in
the permission card, and session-list failure in the sidebar.

Use global banners only for server connection or authentication failures that
affect the whole app.

Every API error view should keep the request ID available for copying even if
the visible message stays concise.

## Empty and first-run states

If the server has no sessions, open directly into the new-session view rather
than a dashboard.

The first screen should make sending the first prompt obvious:

```text
Start a Yoke session

Location   /home/user/dev/yoke
Model      <selected model>

What should Yoke work on?
[                                             ]
```

If sessions exist and `/` has no explicit ID, restore the last selected
session when it still exists. Otherwise choose the most recent active session
or show the new-session view.

## Testing strategy

The web UI's development, runtime, packaging, and browser-test path should not
require Node.js. Do not introduce npm merely to test the browser application.

This is separate from the repository's existing `clients/typescript` contract
job, which already uses Node to regenerate and typecheck the external TypeScript
client. Keep that job. The no-Node constraint applies to building and exercising
the web UI itself, not to deleting an existing independent client-validation
workflow.

### Python contract tests

Extend `tests/yoke/http/` for every server behavior the browser depends on.

Important cases include static shell/assets, browser-route fallback without
shadowing `/api/v1/*`, archived persistence/filtering, SSE auth/formatting,
reconnect/resync behavior, small session summaries, permission/question
snapshots, queue/tree revision conflicts, and public redaction.

### Browser module tests

Keep reducer, router, duration, collection, and local-state logic in pure
modules that can execute against deterministic fixtures in a browser test
page.

### End-to-end tests

When the UI stabilizes, prefer Python Playwright rather than a Node test
project.

High-value flows are:

```text
create a session and send the first prompt
switch between two running sessions
see one become Done while another is selected
answer a permission/question request from attention state
queue and reorder follow-up prompts
open a tool trace
preview and execute tree navigation
settle and reopen a session
disconnect and resynchronize
select a session from the mobile drawer
```

## HTTP additions likely required

Most of the first UI is already covered by the current HTTP API. Keep additions
narrow.

Likely additions:

```text
archivedAt on SessionInfo
archived mutation on SessionPatchRequest
archived filter on listSessions
typed/discriminated public event payloads
possibly a lightweight changed-files/diff projection
possibly a browser auth bootstrap route
```

Do not add API endpoints for features that already exist. In particular, the
current server already has command metadata, provider/model discovery, skill
activation, tool enablement, MCP policy editing, filesystem discovery, uploads,
permissions/questions, process control, retained tool output, queue editing,
session history, and tree navigation.

Add a browser-oriented query only when it prevents expensive reconstruction,
N+1 requests, or exposure of internal data.

## Existing API to reuse

The current HTTP layer already exposes the main building blocks:

```text
session list with search, directory, pinned, order, and cursor
session create/get/patch/fork
active runtime snapshot
paged active-branch messages
context projection
session tree and navigation preview
revision-protected tree navigation
provider/model selection
compaction
prompt admission
steer/queue delivery
queue editor with revision checks
interrupt and wait
permission requests and replies
question requests and replies
provider/model/tool/skill catalogs
command-palette metadata
filesystem operations
process inspection/control
tool traces
MCP inspection and session/repo/global policy mutation
uploads/images
global event stream
durable per-session event history
capability flags and limits
```

The UI should exercise these contracts instead of creating parallel browser
implementations.

## Implementation sequence

### Phase 1: static shell

Add `src/yoke/web`, package the files, serve `/assets/*`, return the app shell
for browser routes, vendor Preact/HTM, add CSS tokens, and render the basic
two-column shell.

Completion criteria:

```text
installing Yoke includes every asset
starting Yoke serves the UI without Node.js
/api/v1/* behavior is unchanged
/session/<id> loads the shell directly
edit and refresh is the frontend development loop
```

### Phase 2: API and sync core

Implement auth-aware fetch, fetch-based SSE parsing, application store,
bootstrap, reconnect, resync, and deterministic event reduction.

Completion criteria:

```text
one tab uses one global event stream
session summaries and runtime states stay current
reconnect produces a correct snapshot
server.resyncRequired repairs state
```

### Phase 3: sidebar

Implement paged sessions, location grouping, pins, search, status labels,
working duration, unread completion, drafts, project collapse/filtering, and
keyboard switching.

Completion criteria:

```text
several sessions can be understood without opening them
Approval/Input/Failed/Done/Working are distinct
pinned work stays globally visible
unrelated histories are never loaded for sidebar rows
unsent draft survives switching and refresh
```

### Phase 4: timeline and composer

Implement paged messages, turn grouping, Markdown and sanitization, streaming
updates, autoscroll, attachments, model selection, prompt submission,
interrupt, command-palette metadata, and skill activation.

Completion criteria:

```text
a normal coding session can run entirely from the browser
scrolling up disables forced autoscroll
large history loads incrementally
failed submission preserves the prompt
private provider reasoning stays out of the browser
```

### Phase 5: queue and human input

Expose steering, queue editing, permissions, and questions.

Completion criteria:

```text
running sessions offer Steer now and Queue next when supported
queue items can be edited, paused, removed, and reordered
revision conflicts reload instead of silently merging
permission/question requests affect sidebar attention state
answers update without full-page reload
```

### Phase 6: inspectors

Implement Tree, Tool, Process, MCP, tool-configuration, and Diff modes. Use the
existing session MCP and tool-policy endpoints rather than treating those as
future backend work.

Completion criteria:

```text
tool summaries open full public trace detail
process controls use the existing process API
tree navigation always previews and checks revision
session tool enablement updates immediately
MCP server/tool policy can be inspected and changed at supported scopes
diffs open contextually
closing inspector returns width to conversation
```

### Phase 7: settled history

Add server-backed archive state and expose it through a sidebar section labeled
`Settled`.

Completion criteria:

```text
settling removes a session from active project groups
reopening preserves session identity/history
archive state survives server/browser restart
settled history paginates
runtime idle and archived remain separate concepts
```

### Phase 8: hardening

Exercise large synthetic sessions, many concurrent runtimes, reconnects,
responsive layout, accessibility, keyboard navigation, and a built wheel.

Completion criteria:

```text
wheel contains all assets and licenses
no production request depends on npm or a public CDN
200 session summaries remain responsive
streaming does not rerender unrelated sidebar rows
refresh during active work reconstructs correct state
optional capabilities can be disabled without breaking the shell
```

## Design rules for review

### Attention beats activity

A session waiting for the user is more important than one working normally. A
newly finished session is more important than one already reviewed.

### Conversation gets the width

The normal state is sidebar plus conversation. Extra detail appears on demand.

### Sidebar is the work manager

Do not add session tabs to compensate for a weak sidebar. Fix the sidebar with
pinning, grouping, search, attention state, and keyboard navigation.

### Runtime and organization are separate

`running`, `waiting_input`, and `error` describe the agent. `pinned`,
`archived`, `unread`, and `draft` describe how the user organizes work. The UI
may label archived work `Settled` without reusing that term in the HTTP model.

### Server owns durable truth

Browser state can improve navigation and presentation. Durable Yoke session
state belongs on the server.

### One stream, many views

Reduce one process-wide event stream into shared state used by sidebar,
timeline, queue, and inspector views.

### Summaries first

Keep session rows and timeline activity compact. Load message history, large
tool output, traces, diffs, and process logs when the user asks for them.

### No frontend ceremony

Adding or editing the UI should require a text editor and browser refresh, not
a JavaScript package manager or production frontend build.

## Definition of done for the first useful release

The first release is done when one person can start Yoke, open the browser,
launch several sessions, leave them running, and later understand the state of
all of them from the sidebar without opening each one.

From the browser the user can create sessions, send prompts and attachments,
switch immediately, see working duration and human attention, recognize
completed unreviewed work, pin/search sessions, view streamed output, steer or
queue follow-ups, edit the queue, answer permissions/questions, interrupt work,
inspect tools/processes/tree/diffs, settle old sessions, and recover correct
state after refresh or reconnect.

The deployment constraint remains:

```text
git clone
install Yoke with its Python dependencies
run Yoke
open browser
```

There is no frontend setup command and no frontend production build.
