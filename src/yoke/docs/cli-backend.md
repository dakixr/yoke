# CLI session backend

This page is for contributors working on the terminal CLI's connection to the
daemon. It maps the CLI's backend objects to daemon routes and records the
rules the terminal follows so it never runs work itself. The protocol it
implements is in [daemon-client.md](daemon-client.md) and
[http-reference.md](http-reference.md).

Every terminal entry point, interactive or headless, talks to the per-user
daemon. There is no in-process agent path. `yoke.cli.run_cli` takes no agent
argument and always uses the daemon. The SDK's `RuntimeAgent` still runs
in-process for library users, but the CLI never constructs one.

## Layout

| Module | Owns |
| --- | --- |
| `cli/backend/session_view.py` | Immutable input and runtime views, event reduction and revision gaps. |
| `cli/backend/connection_state.py` | Connection phase, attachment generation and the session-scoped cursor. |
| `cli/backend/connection.py` | `SessionConnection`: HTTP over the discovered endpoint, the attachment and the filtered SSE subscription. |
| `cli/backend/startup.py`, `cli/backend/headless.py` | Headless and interactive startup through the daemon. |
| `cli/backend/daemon/session.py` | `DaemonSessionBackend`, the object the UIs drive. |
| `cli/backend/daemon/presentation.py` | `DaemonActiveSession`, the session metadata view. |
| `cli/backend/daemon/turn.py` | `DaemonSubmittedTurn`, one admitted input's events and result. |
| `cli/backend/daemon/queue.py` | Queue snapshots and revision-checked edits. |
| `cli/backend/daemon/inspection.py` | `DaemonToolInspection` and process queries. |
| `cli/backend/daemon/catalog.py`, `tree.py`, `compaction.py`, `images.py` | Catalog, tree, compaction and image adapters. |
| `cli/backend/events.py` | `event_from_sse` and `agent_event`. |
| `cli/backend/values.py` | Shared terminal values: `CompactionSnapshot`, `TreeNavigationResult`, `LoadedPromptQueue`, `PromptQueueRevisionConflict`, `ActiveSessionRef`, `EventRenderer`. |
| `cli/interactive/queue/` | Queue presentation and snapshot installation. |
| `cli/interactive/session_observer.py` | Rendering of other clients' turns and daemon-promoted inputs. |

The `backend/daemon` modules contain no terminal rendering. Each adapter
validates the daemon's response models at its own boundary and converts them to
terminal values. `CommandContext.backend` carries the daemon client into slash
commands. `CommandCallbacks` holds terminal capabilities only. `cli/runtime`
keeps terminal selectors, output gating and pure tree-view helpers.

## Connection and events

`SessionConnection` attaches with `kind: "cli"`, `mode: "full-access"`, a fresh
copy of the terminal's environment and its working directory. On connect, and
after `server.resyncRequired`, it:

1. opens the filtered stream `GET /event?sessionID={id}&client={owner}`,
2. reads every newer history page and the active transcript,
3. drops durable events it has already seen, by sequence,
4. reads `GET /session/{id}/input?live=true` and `GET /session/active`,
5. installs both snapshots before marking the connection ready,
6. publishes the local `client.resynced` event with the recovered messages.

`ConnectionState` owns discovery, attachment, streaming, syncing, readiness,
disconnection and closure. Each completed sync advances its generation. A
session switch resets the cursor and advances a separate identity epoch in one
operation. GET recovery refuses to retry across an identity change. Mutations
are never retried implicitly. `connection_events` publishes
`client.connection {phase, generation}` separately from the retained session
feed, whose `client.disconnected` and `client.resynced` aliases keep their order.

When the stream drops, it repeats discovery, the skew check and attachment with
a new environment copy. It counts completed reattachments. After an uncertain
admission, the terminal can wait for a newer attachment and then replay the
same input ID. It never replays any other mutation. `close()` cancels the
reader, detaches only its own owner and closes its subscriptions.

`submit` returns a `DaemonSubmittedTurn`. The UI iterates its events as
`{type, payload}` dictionaries, then reads the result. `event_from_sse` builds
those dictionaries from an SSE envelope's `type` and `data`. `agent_event`
translates daemon event names back to the renderer's callback names, using the
table in [daemon-client.md](daemon-client.md#event-names). The connection adds
a local `renderedLocally` annotation before fan-out. Provider events and their
order are unchanged. A session subscription can watch the same stream independently
and has an explicit unsubscribe callback.

Rendering runs in the UI's turn worker after admission succeeds. The daemon
owns execution, checkpoints, skill state, promotion, retirement, titles and
workspace leases.

## Who renders what

The terminal registers each locally submitted input ID before it sends the
admission. The submitting worker renders that input's live events and its final
answer. `SessionObserver` renders everything else, meaning inputs the daemon promotes
from the queue and turns that other clients started. It skips the registered
IDs for that rendering attempt. Durable sequence deduplication stops replays
after a reconnect. Terminal input phases discard ownership, and a failed
workspace attempt releases ownership so a later queue resume can render it.
Each observed input has its own renderer lifetime. Settlement and workspace
pauses close only that input's renderer; failures are reported as failures.

The daemon alone promotes queued inputs. Queue snapshots in the terminal are
presentation state. Admitting a stale snapshot entry again would run the turn
twice, so the terminal never dequeues an item to run it locally.

A workspace failure can pause an input without settling it. The turn reader
ends that attempt on `session.runtime.failed`, shows the error, and leaves the
paused input in the daemon. For settled failures it waits for the same event,
which follows `session.prompt.settled`. Interruption keeps the terminal's
display fence and sends `POST /session/{id}/input/{inputID}/cancel`. The editor
captures the visible input ID before scheduling HTTP, so a delayed Stop cannot
interrupt its successor. Daemons without the lifecycle capability use
`POST /session/{id}/interrupt`. The daemon saves the
interrupted turn and retires the workers. A stopped result stays a stopped
outcome, including when the runtime raises `AgentStoppedError` before it
returns.

## Session view

`SessionViewStore` performs no I/O. It reduces `session.input.changed`,
`session.queue.held` and `session.active.changed` into immutable snapshots.
Queued and paused inputs form the queue in position order. Promoted and running
inputs identify the active input. Busy means the runtime state is neither idle
nor error, including operations and waiting for human input. Runtime start time
drives elapsed time. The toolbar, spinner, submission choice and stop key read
this view in both interactive modes.

Duplicate input revisions are ignored. A gap latches a resync request until
the connection worker installs a fresh snapshot. Runtime events at or below
the installed runtime revision are ignored. Reconnection installs the new
runtime snapshot even when a restarted daemon has reset its runtime revision.
Terminal inputs leave the view, so the terminal keeps no ever-growing set of
submitted IDs. Render threads and local display counters describe rendering
only; they do not decide whether the daemon is busy.

The capability check is `features.inputLifecycle`. Without it, legacy prompt
events supply the fallback projection and queue snapshots retain the started
filter. That fallback is confined to the backend. It does not hide queued or
workspace-paused inputs when the capability is present.

Queue and image edits serialize on a separate queue-edit lock. Snapshot reads,
queue commits and cancellation run outside the lock used to render the toolbar.

## Route mapping

`{id}` is the backend's selected session.

| Backend operation | Daemon request |
| --- | --- |
| `submit(prompt, ..., delivery="steer" or "queue")` | `POST /session/{id}/prompt`, `GET /event?sessionID={id}`, `POST /session/{id}/wait`, `GET /session/{id}/message` |
| `events.subscribe`, `event_from_sse` | `GET /event?sessionID={id}` |
| `stop_running`, `stop_input` | `POST /session/{id}/input/{inputID}/cancel`; legacy fallback uses `/interrupt` |
| `resume_queue` | `POST /session/{id}/queue/resume` |
| `load_prompt_queue_state`, `commit_prompt_queue`, `clear_prompt_queue` | `GET` and `PATCH /session/{id}/queue` |
| `session`, `provider_label`, `thinking` | `GET /session/{id}`, `GET /model` |
| `set_thinking`, `persist_metadata`, `switch_model` | `POST /session/{id}/selection` |
| `compact` | `POST /session/{id}/compact`, then `POST /session/{id}/wait` |
| `pin`, `title` | `PATCH /session/{id}` |
| `regenerate_title` | `POST /session/{id}/title/regenerate` |
| `fork`, `new` | `POST /session/{id}/fork`, `POST /session` |
| `tree`, `navigate`, `label_entry` | `GET /session/{id}/tree`, `POST /session/{id}/tree/navigate`, `PATCH /session/{id}/tree/{entryID}` |
| `models` | `GET /model`, `GET /provider` |
| `available_skills`, `activate_skill` | `GET /skill`, `POST /session/{id}/skill/{name}/activate` |
| `tools().report`, `active_names`, `select` | `GET /tool?sessionID={id}`, `PATCH /session/{id}/tool` |
| `mcp(root).config`, `list_tools` | `GET /session/{id}/mcp?includeTools=true&includeDisabledTools=true` |
| `mcp(root).set_enabled`, `toggle_tool`, `refresh` | `PATCH /session/{id}/mcp/{server}`, `PATCH /session/{id}/mcp` |
| `processes().snapshots`, `subscribe` | `GET /process?sessionID={id}`, `GET /event` |
| `processes().agent_runs` | `GET /agent-run?sessionID={id}` |
| `DaemonToolInspection.snapshots`, `call`, `output` | `GET /session/{id}/tool-call`, `GET /session/{id}/tool-call/{callID}`, `GET /session/{id}/tool-call/{callID}/output?afterSeq=` |
| relocation on resume | `POST /session/{id}/relocate` |

Notes on specific commands:

- `/info` refreshes `GET /session/{id}` before rendering. It uses
  `databasePath`, `contextWindowTokens`, `tree.entryCount` and `time`.
  Context usage comes from the saved usage snapshot, not from `/context`. The
  terminal does not estimate tokens for the editor draft.
- Manual compaction waits for `/wait` before it replaces the UI history.
- `/tools` and `/mcp` read daemon catalogs. Tool inspection sets
  `includeDisabledTools=true` so disabled tools stay visible and can be turned
  back on. Edits to repository and global MCP or tool config files happen in
  the terminal process, followed by `PATCH /session/{id}/mcp` to reload while
  keeping the session's other tool choices.
- The process inspector shows the output tails from `GET /process`. The API
  also supports output paging, stdin and signals, which the terminal does not
  use. Tool output inspection does not consume output.
- `/new` and `/fork` move the event stream and the owner attachment to the new
  session and reset the durable sequence cursor.

## Queue drafts

The terminal installs new inputs, queue edits and image-draft changes in one
`PATCH /session/{id}/queue` against one `expectedRevision`, using `admissions`,
`pendingImages`. Submitting a new queued prompt sets `resume: true`, including
when the daemon became idle just before admission. Queue-manager and image
edits use `resume: false`. A failed patch rolls back the admissions,
upload pins and events together. `pendingImages` holds draft image paths and
comes back on `GET /queue`, including after reconnect. Submitted images use
durable upload references instead. If the patch conflicts, the queue manager
reloads the queue and reports the conflict instead of overwriting the other
edit.

Queued `/skill <name>` commands carry the optional `prompt.skill` field. It is
a private CLI extension and is left out of the published schema. When the
input reaches its turn, the daemon adds the durable skill marker without a
provider call, so later queued prompts see the skill's instructions. Ordinary
prompt text, including a headless literal `/skill ...`, has no tag and goes to
the model as text. Queue edits and admission replays keep the tag in the
input's fingerprint.

A promoted input that the daemon blocks returns to the queue paused at its
original position, so earlier paused items keep their order. The toolbar shows
queue holds with their reason. `Ctrl+R` calls the queue resume route, which
clears the hold without unpausing individual inputs.

Enter during an active turn admits with `delivery: "steer"` and `resume: true`.
It does not send a separate interrupt. The daemon retires and promotes under
its gate, ahead of ordinary queued inputs. The steering differential test
compares this path with interrupt then queue, including serialized provider
requests, cache identity and the stored tree.

## Images

Terminal image attachments set `imageProjection: "cli"`. The daemon then builds
the provider message in the terminal format, which saved terminal sessions
already use. Labels start at one per prompt, and images carry no attachment
name. Changing this would change provider-visible bytes and cache identity for
those sessions. Images a
model cannot accept follow the runtime image projection instead of the
browser's admission rejection.

## Startup, resume and model checks

Startup holds a temporary daemon lease until the session's event stream is
ready. Daemon startup and the CLI share the saved-selection logic in
`cli/providers/state.py`. Neither imports CLI execution helpers.

Headless entry points run startup resolution, upload image bytes, admit with a
stable input ID and `clientID`, render the status vocabulary, and read their
input's answer from the transcript.

Fresh workspace paths are validated before startup. Resume uses the saved
workspace for attachment and provider discovery. The chooser's root is only a
filter. A saved session without a directory needs explicit relocation.
Interactive resume replays the saved transcript before accepting a new turn,
and it does not rewrite an unchanged selection or skill snapshot. If the saved
provider was removed, interactive resume shows the recovery warning and picks
an available provider. Explicit selections and headless startup stay strict.

Interactive `--fork` creates the copy through the daemon before attaching.
Strict explicit selections and headless saved selections are checked against
the daemon's model catalog before `--fork` copies anything, so an invalid model
leaves the source and the session list unchanged.

Model selection saves any automatic compaction before it returns. If the
provider switch works but saving the selection fails, the daemon returns
`selection_metadata_not_saved` with the effective selection. The terminal keeps
the switch and reports the failure.

## Usage attribution

Usage records name the kind of attachment that admitted each input: `cli`,
`web` or `acp`. Reattaching or a later client cannot change an admitted input's
attribution, and automatic title requests inherit it. A session created with a
client stores that client's kind in `sessions.origin`. Creating without a
client records `web`. Resuming keeps the original origin.
