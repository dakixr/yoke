# CLI session backend

Every terminal entry point uses `DaemonSessionBackend` over `SessionConnection`.
The UI accepts `DaemonActiveSession` metadata and daemon backend types directly.
There is no local agent adapter or agent-injection parameter. Queue snapshots
and compare-and-swap edits live in `backend/daemon/queue.py`; inspector queries
live in `backend/daemon/inspection.py`. These modules contain no terminal rendering.
`CommandContext.backend` carries the daemon client into slash commands;
`CommandCallbacks` contains terminal capabilities only.

## Events and ownership

`submit` returns a `DaemonSubmittedTurn`. The UI iterates dictionaries with `type` and
`payload`, then reads the result. Public event names match the daemon publisher;
`event_from_sse` extracts the same dictionaries from an SSE envelope's `type`
and `data`. `agent_event` translates the public names into the existing renderer
vocabulary. Model, provider recovery, warnings, errors, messages, compaction and
tool events retain their payloads and order. A session subscription can observe
the same stream independently and has an explicit unsubscribe callback.

The daemon producer runs independently. Rendering occurs in the UI's turn
worker, after admission has succeeded. The daemon owns execution, checkpoints,
skill state, turn promotion, retirement, titles and workspace leases.

Logical interruption fences and queue presentation remain in the interactive
package. The daemon alone promotes its queue. A terminal never dequeues a
snapshot into another admission. Queue snapshot-installation helpers live in
`interactive/queue/snapshot.py`. `cli/runtime` retains only terminal selectors,
output gating and pure tree-view helpers.

## Route mapping

`backend/connection.py` now supplies HTTP over the discovered Unix socket and
an owned, filtered SSE subscription. It opens the live subscription before
reading durable history and transcript pages, deduplicates durable events by
sequence, and publishes `client.resynced` with the recovered transcript. Stream
loss repeats discovery and attachment with a fresh environment snapshot.
`close()` cancels the reader, detaches only its owner, and closes subscriptions.
Headless command entry points use `backend/startup.py` and `headless.py`.
They use startup resolution, upload image bytes, admit with a stable input ID
and `clientID`, render the existing status vocabulary, and retrieve the answer
for their input from the transcript. Interactive entry points use the daemon
adapter. Startup holds a temporary daemon lease
until the session event stream is ready.

`{id}` means the backend's selected session ID.

| Backend operation | HTTP equivalent |
| --- | --- |
| `submit(prompt, ..., delivery="steer" or "queue")` | `POST /session/{id}/prompt`; `GET /event?sessionID={id}`; `POST /session/{id}/wait`; `GET /session/{id}/message` |
| `events.subscribe`, `event_from_sse` | `GET /event?sessionID={id}` |
| `interrupt` | `POST /session/{id}/interrupt`; daemon owns the continuation checkpoint |
| `load_prompt_queue_state`, `commit_prompt_queue`, `clear_prompt_queue` | `GET` / `PATCH /session/{id}/queue`, preserving queue revisions |
| `session`, `provider_label`, `thinking` | `GET /session/{id}` and `GET /model` |
| `set_thinking`, `persist_metadata`, `switch_model` | `POST /session/{id}/selection` |
| `compact` | `POST /session/{id}/compact` plus events |
| `pin`, `title` | `PATCH /session/{id}` |
| `regenerate_title` | `POST /session/{id}/title/regenerate` |
| `fork`, `new` | `POST /session/{id}/fork`, `POST /session` |
| `tree`, `navigate`, `label_entry` | `GET /session/{id}/tree`, `POST /session/{id}/tree/navigate`, `PATCH /session/{id}/tree/{entry_id}` |
| `models` | `GET /model`, `GET /provider` |
| `available_skills`, `activate_skill` | `GET /skill`, `POST /session/{id}/skill/{name}/activate` |
| `tools().report`, `active_names`, `select` | `GET /tool?sessionID={id}`, `PATCH /session/{id}/tool` |
| `mcp(root).config`, `list_tools` | `GET /session/{id}/mcp?includeTools=true` |
| `mcp(root).set_enabled`, `toggle_tool`, `refresh` | `PATCH /session/{id}/mcp/{server_name}` |
| `processes().snapshots`, `subscribe` | `GET /process?sessionID={id}`, `GET /event`; periodic polling is supported |
| `processes().agent_runs` | `GET /agent-run?sessionID={id}` |
| `DaemonToolInspection.snapshots`, `call`, `output` | `GET /session/{id}/tool-call`, `GET /session/{id}/tool-call/{call_id}`, `GET /session/{id}/tool-call/{call_id}/output?afterSeq=` |

`/info` refreshes `GET /session/{id}` before rendering. Its response includes
read-only `databasePath` and `contextWindowTokens`; `tree.entryCount` supplies the
full conversation-entry count, and `time` supplies current saved timestamps.
`context_usage` reads the saved usage snapshot, rather than treating the
`/context` conversation export as token accounting. Editor draft estimation is
not performed in the terminal.

A session observer renders daemon-owned queued inputs and other clients' turns.
Local input IDs are registered before admission and excluded from that observer;
the submitting worker renders their live events and input-specific final answer.
Durable sequence deduplication and settled input IDs prevent replay on reconnect.
The queue, session metadata, submitted turn, tree and inspector adapters own
separate typed response conversions within the `backend/daemon` package.
Its public facade exports the session backend and metadata view. HTTP response
models are validated at each
of those boundaries.

Compaction waits for the daemon operation to settle before replacing UI history.
Interactive and headless relocation use `POST /session/{id}/relocate`. No
terminal entry point constructs a local runtime. The public `yoke.cli.run_cli`
export accepts no agent injection and always uses the daemon. The SDK remains
an in-process library as specified by the design.

The process inspector currently displays snapshot output tails, corresponding
to the output fields on `GET /process`; the API also supports output pagination,
stdin and signals. It has no interactive signal or stdin shortcuts to migrate.
Tool output inspection is non-consuming. Persisted repo/global tool and MCP
config edits remain client-side, followed by a backend refresh.

## Ownership after Phase 5

The local session backend, turn executor, invocation decorators and workspace
ContextVars are deleted. The UI no longer sends checkpoint callbacks or calls
local persistence, turn-promotion or retirement methods. Daemon admission and
execution own those operations under `http/runtime`.

Shared terminal values (`CompactionSnapshot`, `TreeNavigationResult`,
`LoadedPromptQueue`, queue revision conflicts, `ActiveSessionRef` and
`EventRenderer`) live in `cli/backend/values.py`. They do not import an executor.
HTTP startup reads saved selection defaults through `cli/providers/state.py`.
All keyboard bindings and the shortcuts table are unchanged.

Daemon clients validate fresh workspace paths before startup. Resume uses the
saved workspace for attachment and provider discovery; the chooser's root is
only a filter. Rootless saved records require explicit relocation. Interactive
`--fork` creates the copy through the daemon before attaching its terminal.
A stopped runtime result stays a stopped terminal outcome, including when the
runtime raises `AgentStoppedError` before returning a result.

Strict explicit selections and headless saved selections are checked against the
daemon model catalog before `--fork` creates a durable copy. Invalid models leave
the source and session list unchanged.

The tools and MCP menus accept daemon catalogs directly. They no longer test
for an in-process runtime. Queue drafts, including pending image paths, persist
through the daemon; one revision-checked PATCH installs new admissions and
edits together. A terminal never removes a queue item to execute it locally.
