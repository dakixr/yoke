# CLI session backend

All production CLI entry points use the daemon. The interactive UI uses
`yoke.cli.backend.SessionBackend` for runtime operations, the queue functions in
`yoke.cli.backend.queue` for durable queue snapshots and compare-and-swap edits,
and the inspector query objects in `yoke.cli.backend.inspection`. The backend
contains no terminal rendering. `CommandContext.backend` carries the boundary
into slash commands; `CommandCallbacks` contains terminal capabilities only.

`SessionBackend.wrap` adapts compatibility test doubles and reuses an existing
backend while the selected session changes. Production construction uses
`DaemonSessionBackend` over `SessionConnection`. The `AgentRunner` protocol and
its import alias are gone.
`runtime/contracts.py` contains only opted-in call-shape adapters for existing
injected test doubles, with no runner protocols.

## Events and ownership

`submit` returns a `SubmittedTurn`. The UI iterates dictionaries with `type` and
`payload`, then reads the result. Public event names match the daemon publisher;
`event_from_sse` extracts the same dictionaries from an SSE envelope's `type`
and `data`. `agent_event` translates the public names into the existing renderer
vocabulary. Model, provider recovery, warnings, errors, messages, compaction and
tool events retain their payloads and order. A session subscription can observe
the same stream independently and has an explicit unsubscribe callback.

The daemon producer runs independently. Rendering occurs in the UI's turn
worker, after admission has succeeded. Accepted and retired outcomes go through
backend methods so the UI does not inspect runtime forks.

The former in-process implementation remains in `cli/runtime` for differential
tests until the final cleanup; production command dispatch does not call its execution entry points. Pure
selector, rendering and compatibility types still live in `cli/runtime`.
Logical interruption fences and queue presentation remain in the interactive
package. The daemon alone promotes its queue; a terminal never dequeues a
daemon snapshot into another admission. Their persistence and runtime operations cross the
backend boundary; daemon admission owns those operations. The unused `interactive/turn_runner.py` shim is gone. Queue snapshot-installation
helpers live in `interactive/queue/snapshot.py`; persistence operations are
imported from the backend.

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
| `interrupt`, `interrupt_snapshot` | `POST /session/{id}/interrupt`; daemon owns the continuation checkpoint |
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
| `ToolInspection.snapshots`, `call`, `output` | `GET /session/{id}/tool-call`, `GET /session/{id}/tool-call/{call_id}`, `GET /session/{id}/tool-call/{call_id}/output?afterSeq=` |
| `persist`, `sync_skills`, `checkpoint_skills`, `promote_turn`, `retire_turn` | Internal daemon work during prompt admission/execution; clients read messages and events |
| `require_workspace`, `submission_context` | Admission validation and local executor ownership; removed from the client when daemon admission owns this |

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
production terminal command calls `runtime.cli.run_cli` or `run_resume_cli`;
those Python compatibility functions remain for injected and differential tests.
The public `yoke.cli.run_cli` export accepts no agent injection and always uses
the daemon. The SDK remains an in-process library as specified by the design.

The process inspector currently displays snapshot output tails, corresponding
to the output fields on `GET /process`; the API also supports output pagination,
stdin and signals. It has no interactive signal or stdin shortcuts to migrate.
Tool output inspection is non-consuming. Persisted repo/global tool and MCP
config edits remain client-side, followed by a backend refresh.

## Runtime coupling inventory

This inventory records the dependencies before the refactor at `2829e85`.
Files in a group share the indicated dependency; rendering-only files are omitted.

| Original callers | Original runtime dependency | Current owner |
| --- | --- | --- |
| `interactive/__init__.py`, `basic.py`, `common.py`, `command_host.py`, `commands.py`; `prompt/{app,control,loop,compaction,cancellation,inspectors,turns}.py`; `session_commands.py`; `turn_runner.py` | `AgentRunner` annotations/imports; some callers also checked `RuntimeAgent` | Backend boundary and shared prompt/outcome values; no interactive runtime type checks |
| `interactive/turn_runner.py` | `prepare_turn_agent`, title generation, usage context, `execute_turn`, agent-state capture and workspace validation | `backend/turns.py` |
| `interactive/basic.py`, `prompt/turns.py` | Skill copying, checkpoint/session saves, promoting runtime forks, retiring runtime resources | `SessionBackend` checkpoint/lifecycle operations |
| `interactive/prompt/{app,loop}.py`, `common.py` | Provider/config introspection, effort edits, available skills and context estimates | `SessionBackend` metadata/skills/context operations |
| `interactive/prompt/{cancellation,control}.py`, `basic.py` | Stop events, logical interrupt snapshot, background stop checkpoint | Backend interrupt/snapshot/persist operations; UI still owns logical turn fencing |
| `interactive/slash_commands.py`, `session_commands.py` | Direct store pin writes, title/fork/new/compact operations, session save and database path | `SessionBackend` session operations |
| `interactive/model_commands.py` | Provider switch, state capture, metadata save and private `_context`/log inspection | `SessionBackend.switch_model`; runtime-owned public `conversation_marker` avoids copying a large transcript |
| `interactive/tree_command.py`, `tree_selector.py` | Tree read/navigation/label writes; tree projection values | Mutations/queries through backend; selector retains pure tree-view helpers |
| `interactive/skill_commands.py` | Skill registry/activation, active-skill writes, `SessionTree` construction and runtime hydration | `SessionBackend.activate_skill` |
| `interactive/tools_menu.py` | Tool report, live tool map, selection installation | `ToolInventory`; UI retains rows/scopes/config-file writes |
| `interactive/mcp_menu.py`, `mcp_menu_helpers.py` | Tool-session MCP policy, `McpManager` discovery, refresh and live tool filtering | `McpInventory`; UI retains persisted config editing |
| `interactive/process_commands.py`, `agents.py`, `prompt/inspectors.py`, `process_inspector/app.py` | `CommandProcessManager`, snapshots, subscriptions and agent-run registry | `ProcessInspection`; no manager is passed to an inspector |
| `interactive/prompt/inspectors.py`, `prompt/app.py`, `tool_inspector/trace.py` | Transcript/live trace merging, trace revisions and subscriptions | `ToolInspection` query seam; trace DTOs and event recording remain presentation data |
| `interactive/queue/{__init__,images,mutations,persistence}.py`, `prompt/{app,loop}.py` | Queue snapshot loading, transactions and CAS persistence through `ActiveSession.store` | `backend/queue.py`; compatibility imports and UI queue-state installation remain |
| `interactive/{commands.py,prompt/submission.py,queue/mutations.py}` | Workspace validation before admission/session changes | Backend validation method; exception values remain UI-readable |
| `interactive/prompt/lifecycle.py` | Workspace-owner `ContextVar` copied into command executor | Backend `submission_context` |
| `cli/runtime/base.py`, `contracts.py` | Runner protocols, dispatch adapters and direct runtime execution | No runner protocols; native execution preserved, injected-test call adapters retained |
| `cli/runtime/{cli,startup,session,resume,metadata,title,stats,compaction}.py` | Runtime construction, provider state, session store, save/hydration/title/compaction and usage | Existing in-process implementation, behind backend for interactive callers; headless/bootstrap remain here |
| `cli/runtime/{turn_agent,lifetime,workspaces}.py` | Runtime fork/cleanup, process-manager lifetime, workspace leases and store reads | Existing implementation called by backend; Phase 5 deletes it |
| `cli/runtime/{tree,tree_view}.py` | Session-tree projection, store navigation/metadata writes, branch-summary provider requests | Existing implementation called by backend; selector uses only pure view helpers |
| `cli/runtime/selector/**`, `terminal_output_gate.py` | Terminal selection/output helpers, with duplicated cursor rules | Pure UI utilities; cursor rules now live in `cli/list_cursor.py` |

`/agents` is a Rich table with no selection or scrolling state in this baseline.
MCP and tools menus share the generic selectors. Their cursor rules, the tree
selector and both inspectors now use `ListCursor`. The helper owns clamping,
movement, viewport resizing and identity preservation during refresh. Inspectors
use owned display-state dataclasses instead of attribute-list render/mouse/chrome
protocols. Keyboard bindings and the shortcuts table are unchanged.

Shared terminal result values (`CompactionSnapshot`, `TreeNavigationResult`,
`LoadedPromptQueue`, queue revision conflicts and checkpoint callbacks) live in
`cli/backend/values.py`. Daemon adapters import these without loading the local
turn executor. HTTP startup reads saved selection defaults through
`cli/providers/state.py`.

Daemon clients validate fresh workspace paths before startup. Resume uses the
saved workspace for attachment and provider discovery; the chooser's root is
only a filter. Rootless saved records require explicit relocation. Interactive
`--fork` creates the copy through the daemon before attaching its terminal.
A stopped runtime result stays a stopped terminal outcome, including when the
runtime raises `AgentStoppedError` before returning a result.

Strict explicit selections and headless saved selections are checked against the
daemon model catalog before `--fork` creates a durable copy. Invalid models leave
the source and session list unchanged.
