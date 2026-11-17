# HTTP API reference

This is the endpoint reference for the Yoke daemon's HTTP API, for developers
who already know the concepts in the [HTTP API guide](http.md). Endpoints are
grouped by resource. Each entry lists the method and path, what it does, its
main request and response fields, and the error codes specific to it.

The OpenAPI document at `GET /api/v1/openapi.json` is the full contract,
including every schema. This page lists the fields you usually need.

## Conventions

- Every path starts with `/api/v1`. The tables omit the prefix.
- Field names and query parameters are camelCase.
- Successful responses wrap the payload in `data`. List responses add
  `cursor: {next, previous}` and sometimes `total`.
- Cursors are opaque strings. Pass `cursor.next` back unchanged with the same
  filters. `previous` is always null.
- Times are ISO 8601 strings with a UTC offset.
- `{id}` is a session ID.

These errors can come from any authenticated route and are not repeated below:

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | `unauthorized` | Missing or wrong bearer token on a TCP request. |
| 404 | `session_not_found` | The `{id}` in the path, or a `sessionID` query, names no session. |
| 422 | `validation_error` | The request does not match the schema. `details.errors` lists `{loc, type, msg}`. |
| 503 | `daemon_exiting` | The daemon is shutting down. Rediscover it and retry. |

Routes that run work in a session's directory also return
`409 session_workspace_unavailable` when that directory is missing or
unreadable. Routes that change session state while a turn runs return
`409 session_busy` where noted as "idle only".

## Health and discovery

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Liveness and build. Public. Returns `healthy`, `version`, `buildID`, `protocolVersion`. |
| `GET /capabilities` | Feature flags and limits. `limits` has `maxPromptBytes` (1,048,576), `maxAttachmentBytes` (20,971,520) and `historyPageMax` (200). |
| `GET /command` | Slash-command metadata for UIs: `name`, `description`, `usage`, `action`. |
| `GET /openapi.json` | The OpenAPI document. Public. |

## Daemon

These routes exist only on the per-user daemon. A standalone `yoke serve`
returns `404 daemon_mode_required`.

| Method and path | Purpose |
| --- | --- |
| `GET /daemon` | Status: `pid`, `buildID`, `protocolVersion`, `socket`, `port`, `idleExit`, `busy`, `restartPending`, `leases[]` of `{id, client, pid, openedAt}`. |
| `POST /daemon/restart` | Body `{whenIdle?: bool}`. Returns `{action: "restart", scheduled: "now" \| "when_idle"}`. |
| `POST /daemon/stop` | Body `{force?: bool}`. Returns `{action: "stop", scheduled: "now"}`. `force` interrupts running turns. |
| `GET /daemon/lease` | SSE stream that holds a lease. Query `client` (required, 1 to 64 characters) and `pid`. Sends `daemon.lease`, heartbeats every 15 s, then `daemon.exiting` with `reason`. |

Errors: `409 daemon_busy` from restart without `whenIdle` and from stop without
`force` while work runs. `503 daemon_exiting` from a lease opened during
shutdown.

## Sessions

| Method and path | Purpose |
| --- | --- |
| `GET /session` | List sessions. Returns `data`, `cursor`, `total`. |
| `POST /session` | Create a session, or return the existing one for a repeated `id` and directory. |
| `GET /session/active` | Runtime state of every loaded session, as a map from session ID to state. |
| `GET /session/{id}` | One session. |
| `PATCH /session/{id}` | Set `title`, `pinned` or `archived`. |
| `POST /session/{id}/fork` | Copy the session into a new one. |
| `POST /session/{id}/relocate` | Move the session to another existing directory. |
| `POST /session/{id}/selection` | Switch provider, model and reasoning effort. Idle only. |
| `POST /session/{id}/compact` | Start manual compaction. Idle only. Returns 202. |
| `POST /session/{id}/title/regenerate` | Generate a title from the saved conversation. |

`GET /session` query parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `directory` | none | Only sessions in this directory. |
| `search` | none | Case-insensitive substring match on session ID, title and directory. |
| `pinned`, `archived` | none | Filter by flag. |
| `order` | `updatedDesc` | `updatedDesc`, `updatedAsc`, `createdDesc`, `createdAsc`, `lastUserDesc`, `lastUserAsc`. |
| `limit` | 50 | 1 to 200. |
| `cursor` | none | From `cursor.next`. |

`total` counts every session matching the filters, across all pages.

Session object (`SessionInfo`):

| Field | Meaning |
| --- | --- |
| `id`, `title`, `pinned`, `archivedAt` | Identity and organization. `archivedAt` is null unless archived. |
| `location.directory` | Saved working directory. |
| `workspace` | `{status, message}` observed now. Status is `available`, `missing`, `not_directory`, `unreadable`, `unconfigured` or `invalid`. |
| `selection` | `{provider, model, reasoningEffort}`, each nullable. |
| `time` | `{created, updated, lastUserMessage}`. |
| `tree` | `{leafID, entryCount}`. |
| `queue` | `{total, steering, queued, paused, revision}`. |
| `contextUsage` | Latest provider-reported context usage, or null. |
| `contextWindowTokens`, `databasePath` | Read-only metadata. |

`POST /session` request:

| Field | Meaning |
| --- | --- |
| `location.directory` | Required. An existing readable directory. |
| `id` | Optional. Makes creation idempotent. |
| `title` | Optional. Without it the daemon generates a title when the first turn starts. |
| `selection` | Optional `{provider, model, reasoningEffort}`. |
| `startup` | Run the CLI startup resolver and return a `startup` report. Default false. |
| `skills` | Skill names to activate. Implies `startup`. |
| `headless` | With startup, reject an unknown saved model instead of recovering. Default false. |
| `client` | Attach a client in the same request. See [daemon-client.md](daemon-client.md). |

The response is `{data: SessionInfo, startup}`. `startup` is null unless
startup ran, and then has `warnings`, `activeSkills`, `modelSource` and
`toolReport`.

Other request bodies:

| Endpoint | Body |
| --- | --- |
| `PATCH /session/{id}` | `title`, `pinned`, `archived`. At least one field. `pinned` and `archived` cannot be null. |
| `POST /session/{id}/fork` | `fromEntryID`, `id`, `title`, `location: {directory}`, all optional. |
| `POST /session/{id}/relocate` | `directory` (required), `expectedDirectory`. |
| `POST /session/{id}/selection` | `provider`, `model` (required), `reasoningEffort`. |
| `POST /session/{id}/compact` | `{"reason": "manual"}`. |

`/selection` returns `{effective: {provider, model, reasoningEffort}, applies: "immediately"}`.
`/compact` returns `{accepted: true, operationID}`. The others return
`{data: SessionInfo}`.

Errors:

| Endpoint | Status and code |
| --- | --- |
| `POST /session` | 400 `workspace_unavailable`, 409 `session_identity_conflict` (same `id`, other directory), 400 `session_startup_failed`, 409 `session_busy` (startup on a running session) |
| `PATCH /session/{id}` | 400 `empty_mutation`, 400 `invalid_pinned`, 400 `invalid_archived`, 409 `session_busy` or 409 `session_has_pending_work` when archiving |
| `POST /session/{id}/fork` | 404 `entry_not_found`, 400 `workspace_unavailable` (bad `location`), 409 `session_workspace_unavailable` (no `location` and the source directory is gone) |
| `POST /session/{id}/relocate` | 400 `workspace_unavailable`, 409 `session_workspace_busy`, 409 `session_workspace_conflict` |
| `POST /session/{id}/selection` | 409 `session_busy`, 409 `model_context_too_small`, 507 `selection_metadata_not_saved` |
| `POST /session/{id}/compact` | 409 `session_busy` |
| `POST /session/{id}/title/regenerate` | 400 `title_regeneration_unavailable` (empty conversation), 502 `title_regeneration_failed` |

## Prompts, queue and execution

| Method and path | Purpose |
| --- | --- |
| `POST /session/{id}/prompt` | Admit one input. |
| `GET /session/{id}/queue` | Pending inputs and the queue revision. |
| `PATCH /session/{id}/queue` | Apply queue operations and admissions atomically against a revision. |
| `POST /session/{id}/interrupt` | Stop the running turn. Returns `{interrupted, turnID}`. |
| `POST /session/{id}/wait` | Wait until the session is idle. Query `timeoutMs`, 1 to 300,000, default no limit. Returns runtime state. |
| `POST /session/{id}/drain` | Wait until the session's workers exit. Query `timeoutMs`, 1 to 300,000, default 30,000. Returns `{drained, activeWorkers}`. |

`POST /session/{id}/prompt` request:

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | generated | Input ID. Supply it to make retries safe. |
| `prompt.text` | `""` | Up to 1 MiB of UTF-8. |
| `prompt.attachments` | `[]` | Up to 20 `{type: "file", uri, name, mime}` from `/upload`. |
| `prompt.continuation` | false | Run another inference with no new user message. Text and attachments must be empty. |
| `delivery` | `steer` | `steer` interrupts a running turn. `queue` waits for it. |
| `resume` | true | Look for work to start right after admission. |
| `clientID` | none | Owner of an attached client whose environment and tool policy the input uses. |

The response is a receipt: `id`, `sessionID`, `prompt`, `delivery`, `state`
(`admitted`, `promoted` or `removed`), `admittedSeq`, `promotedSeq`,
`timeCreated`. Repeating a request with the same `id`, prompt and delivery
returns the original receipt.

Queue item: `id`, `prompt`, `delivery`, `paused`, `createdAt`,
`state: "admitted"`. `GET /queue` returns `{revision, items, pendingImages}`.
`pendingImages` is draft state that the terminal client keeps with the queue.

`PATCH /session/{id}/queue` request:

| Field | Meaning |
| --- | --- |
| `expectedRevision` | Required. The revision you last read. |
| `operations` | Required list, applied in order. May be empty. |
| `admissions` | New inputs to admit in the same transaction. Same shape as `/prompt`. |
| `pendingImages` | Replace the draft image paths. |
| `resume` | Default true. |

Queue operations:

| `op` | Other fields | Effect |
| --- | --- | --- |
| `update` | `id`, `prompt` | Replace the input's prompt. |
| `setDelivery` | `id`, `delivery` | Switch between `steer` and `queue`. |
| `setPaused` | `id`, `paused` | Pause or unpause. Paused inputs never start. |
| `remove` | `id` | Delete the input. |
| `moveBefore` | `id`, `beforeID` | Reorder. |
| `moveAfter` | `id`, `afterID` | Reorder. |
| `moveToStart` | `id` | Move to the front. |

Runtime state (`ActiveRuntimeInfo`), returned by `/wait` and `/session/active`:
`state` (`idle`, `running`, `stopping`, `waiting_input` or `error`),
`turnID`, `startedAt`, `activity` (for example `Thinking`, `Running tool`,
`Compacting`, `Waiting for a slot`) and `lastError`.

Errors:

| Endpoint | Status and code |
| --- | --- |
| `POST /session/{id}/prompt` | 409 `input_identity_conflict`, 400 `empty_prompt`, 413 `prompt_too_large`, 400 `too_many_attachments`, 400 `invalid_continuation`, 400 `empty_continuation`, 409 `client_not_attached`, 409 `session_workspace_unavailable`, plus the attachment errors under [Uploads](#uploads) |
| `PATCH /session/{id}/queue` | 409 `queue_revision_conflict` (details `expectedRevision`, `actualRevision`), 404 `queue_item_not_found`, 409 `queue_item_not_editable`, 400 `invalid_queue_operation`, plus the `/prompt` errors for `admissions` |

`/wait` returns 200 on timeout with the current state. `/drain` returns 200
with `drained: false` on timeout.

## Events and history

| Method and path | Purpose |
| --- | --- |
| `GET /event` | SSE stream. Query `sessionID` filters to one session. Query `client` (1 to 64 characters) names the stream and makes it a daemon lease. |
| `GET /session/{id}/history` | Durable events after a sequence. Query `after` (exclusive, default 0) and `limit` (1 to 200, default 50). Returns `{data, hasMore}`. |

Event envelope (`PublicEvent`): `id`, `type`, `time`, `sessionID`,
`location: {directory}`, `durable` and `data`. `durable` is
`{aggregateID, seq, version}` for journaled events and null for live ones.
SSE frames set `id:` to the event ID and `event:` to its type.

Durable event types:

| Type | `data` |
| --- | --- |
| `session.created` | `sessionID` |
| `session.updated` | `sessionID`, `title`, `pinned`, `archivedAt` |
| `session.prompt.admitted` | `inputID`, `clientID`, `delivery`, `prompt` |
| `session.prompt.edited` | `inputID`, `delivery`, `prompt` |
| `session.prompt.removed` | `inputID` |
| `session.prompt.promoted` | `inputID`, `delivery` |
| `session.prompt.settled` | `inputID`, `outcome`: `completed`, `stopped`, `failed` or `recovered` |
| `session.queue.updated` | `revision` and the change |
| `session.message.updated` | saved turn: `inputID`, `turnID`, `status`, `leafID` |
| `session.interrupted` | `inputID`, `turnID`, `reason` |
| `session.runtime.failed` | `inputID`, `turnID`, `error` |
| `session.selection.changed` | the new selection |
| `session.skill.activated` | the skill |
| `session.compaction.started`, `session.compaction.ended` | manual compaction |
| `session.tree.updated` | `revision` and the changed entry |
| `session.workspace.relocated` | `sessionID`, `previousDirectory`, `directory` |

Live event types:

| Type | Meaning |
| --- | --- |
| `server.connected` | First frame. `serverInstanceID`. |
| `server.resyncRequired` | Your stream overflowed and is closing. Catch up from history. |
| `server.stopping` | The daemon is shutting down. |
| `session.active.changed` | Runtime state: `state`, `turnID`, `startedAt`, `error`, `activity`. |
| `session.model.started`, `session.model.ended` | One model call. |
| `session.message.updated` | An assistant message as the agent produced it, with `content` and `phase`. |
| `session.tool.started`, `session.tool.delta`, `session.tool.ended` | Tool lifecycle and output. |
| `session.provider.retry`, `session.provider.rate_limited`, `session.provider.recovered` | Provider retries. |
| `session.notice`, `session.warning`, `session.error` | Agent diagnostics. |
| `session.compaction.*`, `session.context.compacted` | Automatic compaction progress. |
| `session.context.updated` | Context usage. |
| `session.permission.requested`, `session.permission.resolved` | Permission requests. |
| `session.question.requested`, `session.question.resolved` | Questions. |
| `session.plan.updated` | `steps`, `explanation` from `update_plan`. |
| `session.process.updated` | A managed process changed. |
| `session.agent.updated` | An SDK agent run changed. |
| `session.mcp.updated`, `session.tool.config.changed` | Session tool or MCP policy changed. |

Agent progress events keep their payload keys in snake_case and add `turnID`
and `inputID`, except `session.context.updated`.
[daemon-client.md](daemon-client.md#event-names) maps them to agent callbacks.

## Messages, context and tree

| Method and path | Purpose |
| --- | --- |
| `GET /session/{id}/message` | Active-branch transcript. Query `limit` (1 to 200, default 50), `order` (`desc` default, or `asc`), `cursor`. Returns `{data, cursor, snapshotSeq}`. |
| `GET /session/{id}/message/{messageID}` | One message. |
| `GET /session/{id}/context` | Bounded model-visible context. Query `limit` (1 to 1,000, default 100), `maxChars` (1,000 to 2,000,000, default 500,000), `includeSystem` (default false), `includeToolResults` (default true). |
| `GET /session/{id}/tree` | Tree nodes. The first page has the newest nodes and `cursor.next` loads older ones. Query `limit` (1 to 2,000, default 200), `cursor`. |
| `GET /session/{id}/tree/navigation-preview` | What moving to `targetID` would change. Query `targetID` (required), `includeAbandoned` (default true). |
| `POST /session/{id}/tree/navigate` | Move the active leaf. Idle only. |
| `PATCH /session/{id}/tree/{entryID}` | Set or clear a label. Idle only. |

Messages have a `type`:

| `type` | Fields |
| --- | --- |
| `user` | `id`, `kind`, `inputID`, `content`, `timeCreated`, `turnSummary` |
| `assistant` | the same plus `phase` (`commentary` or `final_answer`) and `toolCalls[]` of `{id, name, arguments}` |
| `tool` | `id`, `kind`, `callID`, `result`, `status`, `timeCreated` |
| `control` | `id`, `kind`, `control`, `text`, `timeCreated` |

`content` is a list of `{type: "text", text}` and `{type: "image", name, uri}`.
`turnSummary` is `{durationSeconds, toolCount}` on the last entry of turns that
ran for at least a minute. `snapshotSeq` is the newest durable event sequence
covered by the read.

Context response: `messages[]` of `{role, content, phase, toolCallID}` plus
`totalEntries`, `retainedEntries`, `retainedChars`, `maxChars`, `truncated`.

Tree response: `entries[]`, `leafID`, `revision`, `totalEntries`, `cursor`.
Each entry has `id`, `parentID`, `kind`, `phase`, `createdAt`, `label`,
`preview`, `childCount`, `active` (on the active branch) and `current` (the
leaf).

Tree mutation bodies:

| Endpoint | Body | Response |
| --- | --- | --- |
| `POST /tree/navigate` | `targetID`, `expectedRevision` (required), `branchSummary` | `leafID`, `revision`, `editorText`, `summaryAdded` |
| `PATCH /tree/{entryID}` | `expectedRevision` (required), `label` (null clears) | `entry`, `revision` |

Navigation preview response: `targetID`, `current`, `editorText`,
`abandoned[]` of `{id, kind, preview}` (at most 100), `abandonedTotal`,
`abandonedTruncated`. `editorText` is the prompt text the client can put back in
the composer when the target is a user message.

Errors: 404 `message_not_found`, 404 `entry_not_found`,
409 `tree_revision_conflict`, 409 `tree_root_navigation_unsupported`,
400 `invalid_cursor`, 400 `invalid_cursor_anchor`, 409 `session_busy`.

## Tool calls

| Method and path | Purpose |
| --- | --- |
| `GET /session/{id}/tool-call` | Tool calls in transcript order. Query `status`, `turnID`, `limit` (1 to 200, default 100), `cursor`, `order` (`oldest` default, or `latest`). Returns `{data, cursor, total}`. |
| `GET /session/{id}/tool-call/{callID}` | One call, with `resultProjection`. |
| `GET /session/{id}/tool-call/{callID}/output` | Retained live output. Query `afterSeq` (exclusive, default 0), `limit` (1 to 500, default 200). |
| `GET /tool` | Tool catalog for `directory` or `sessionID`: `name`, `description`, `source`, `sourcePath`, `capabilityID`, `enabled`, `inputSchema`. |
| `PATCH /session/{id}/tool` | Body `{enabled: [...], disabled: [...]}`. Idle only. Returns the enabled names. |

Tool call object: `id`, `toolName`, `status` (`pending`, `running`, `ok`,
`failed`, `cancelled`), `turnID`, `iteration`, `sequence`,
`arguments: {raw, executed}`, `result`, `resultProjection`,
`time: {started, ended, durationMs}`, `output: {latestSeq, retainedChars, truncated}`,
`context[]` and `afterContext[]` of `{role, text}`, and `retention`
(`runtime` for live calls, `session` for saved ones). Missing times stay null.

With `order=latest`, the first page is the newest window and `cursor.next`
pages toward older calls. Rows are always in ascending order. `total` counts
filtered calls before paging. `sequence` is the call's one-based position
before filtering and can be null on live rows.

Output response: `data[]` of `{seq, stream, text}` and
`cursor: {next, truncatedBefore}`.

Errors: 404 `tool_call_not_found`, 400 `unknown_tool`, 400 `invalid_cursor`,
400 `invalid_cursor_anchor`, 409 `session_busy`.

## Processes and agent runs

Processes are the managed commands that tools start. They exist only in daemon
memory.

| Method and path | Purpose |
| --- | --- |
| `GET /process` | Query `sessionID`, `status` (`running`, `exited`, `failed`), `limit` (1 to 200, default 100). |
| `GET /process/{processID}` | One process. |
| `GET /process/{processID}/output` | Query `afterSeq` (exclusive, default 0), `limit` (1 to 500, default 200). Returns `data[]` of `{seq, stream, text}` and `cursor: {next, truncatedBefore}`. |
| `POST /process/{processID}/stdin` | Body `{text}`. Writes to a running process. |
| `POST /process/{processID}/signal` | Body `{signal: "interrupt" \| "terminate"}`. |
| `GET /agent-run` | SDK agent runs owned by `sessionID` (required), newest first. |

Process object: `processID`, `sessionID`, `runtimeSessionID` (the
`session_id` the process tools use), `pid`, `command`, `cwd`, `tty`, `status`,
`exitCode`, `startedAt`, `elapsedMs`, and
`output: {tail, latestSeq, originalBytes, retainedBytes, truncated}`.

Agent run object: `agentId`, `runId`, `name`, `provider`, `model`, `effort`,
`status` (`running`, `completed`, `failed`, `cancelled`, `interrupted`),
`observation` (`live`, `stale`, `lost`), `parentAgentId`, `parentRunId`,
`taskId`, `attempt`, `lastToolName`, `errorType`, `startedAt`, `updatedAt`,
`lastSeenAt`, `finishedAt`, `version` and `typedUsage`. `typedUsage` holds
cumulative token counts, `toolUses` and `durationMs`. Counters a provider did not
report stay absent rather than zero. Runs never include prompts, outputs or
exception messages.

`observation` is independent of `status`. After a daemon restart, a run whose
worker cannot be confirmed becomes `lost`. That does not mean the worker
stopped. Reading runs never starts, stops or consumes anything.

Errors: 404 `process_not_found`, 409 `process_not_running`,
400 `empty_process_input`, 409 `process_input_failed`,
400 `invalid_process_signal`.

## Human input

| Method and path | Purpose |
| --- | --- |
| `GET /session/{id}/permission` | Pending permission requests: `id`, `permission`, `message`, `toolCall`, `createdAt`. |
| `POST /session/{id}/permission/{requestID}/reply` | Body `{reply: "allow" \| "deny", message?}`. |
| `GET /session/{id}/question` | Pending questions: `id`, `question`, `options`, `multiple`, `createdAt`. |
| `POST /session/{id}/question/{requestID}/reply` | Body `{answers: [...]}`. |
| `POST /session/{id}/question/{requestID}/reject` | Decline the question. |

Pending requests live in daemon memory and disappear on restart.

Errors: 404 `permission_not_found`, 400 `invalid_permission_reply`,
404 `question_not_found`, 400 `empty_question_reply`,
400 `invalid_question_reply`.

## Client attachments

| Method and path | Purpose |
| --- | --- |
| `PUT /session/{id}/client` | Attach or replace one client's configuration. |
| `GET /session/{id}/client` | Query `owner` (optional). Returns `{mode, plan, owners}` for that client, or for the most recently attached one, or `data: null`. |
| `DELETE /session/{id}/client` | Query `owner` (required). Detaches that client only. Returns `{detached: true}`. |

`PUT` request (`ClientSessionRequest`):

| Field | Default | Meaning |
| --- | --- | --- |
| `owner` | required | Client identity, 1 to 128 characters. |
| `kind` | `acp` | `cli`, `web` or `acp`. Only `acp` adds the plan and question tools. |
| `mode` | `full-access` | `full-access`, `approval-required`, `auto-accept-edits` or `plan`. |
| `env` | none | Full environment for this client's turns. Kept in memory only. |
| `cwd` | none | Absolute existing directory for commands. |
| `mcpServers` | `[]` | Up to 64 `{name, transport, command, args, env, envVars, url, headers}`. Names must be unique. |
| `additionalDirectories` | `[]` | Up to 32 absolute existing directories. |

The `PUT` response contains only `mode`, `additionalDirectories` and the MCP
server names. Attaching and detaching work while a turn runs. See [daemon-client.md](daemon-client.md)
for the full contract.

## Catalogs and configuration

| Method and path | Purpose |
| --- | --- |
| `GET /provider` | Providers for `directory`: `id`, `ready`, `reason`, `currentModel`, `currentReasoningEffort`, plus `defaultSelection`. |
| `GET /model` | Models for `directory`, filtered by `provider` and `search`: `id`, `name`, `provider`, `contextWindowTokens`, `reasoningEfforts`, `defaultReasoningEffort`, `capabilities: {images, tools}`. |
| `GET /skill` | Skills for `directory`, filtered by `search`: `name`, `description`, `sourcePath`, `active`. |
| `GET /session/{id}/skill` | `{active, available}`. |
| `POST /session/{id}/skill/{skillName}/activate` | Body `{prompt?}`. Idle only. Activates the skill. A non-empty `prompt` is also admitted as a steering input, and its ID is returned as `promptInputID`. |
| `GET /mcp` | MCP servers configured for `directory`. `includeTools=true` lists their tools. |
| `GET /session/{id}/mcp` | The session's MCP servers. Query `includeTools`, `includeDisabledTools`. |
| `PATCH /session/{id}/mcp` | Reload MCP configuration files, keeping the session's tool choices. Idle only. |
| `PATCH /session/{id}/mcp/{serverName}` | Body `enabled`, `enabledTools`, `disabledTools`, `scope` (`session` default, `repo` or `global`). Non-session scopes write the config file and are idle only. |

MCP server object: `name`, `transport`, `scope` (`global`, `repo` or
`unknown`), `sourcePath`, `enabled`, `enabledTools`, `disabledTools`, `status`,
`error`, `tools[]` of `{name, description, inputSchema}` and `truncated`.

Errors: 404 `skill_not_found`, 404 `mcp_server_not_found`,
400 `empty_mutation`, 400 `mcp_config_update_failed`,
404 `location_not_found`, 503 `model_catalog_unavailable` (a requested
provider's catalog failed), 409 `session_busy`.

## Locations and files

| Method and path | Purpose |
| --- | --- |
| `GET /location` | Resolve `directory`: `directory`, `name`, `git: {root, branch}`. |
| `GET /location/recent` | Directories of recent sessions. |
| `GET /location/browse` | List child directories of `path` (absolute or starting with `~`, up to 4,096 characters). Query `limit` (1 to 1,000, default 500). Returns `browseDirectory`, `parentDirectory`, `selectableDirectory`, `separator`, `entries[]` of `{name, directory}`. Includes hidden directories. |
| `GET /fs/list` | Entries of `path` under `directory`: `name`, `path`, `type`, `size`. |
| `GET /fs/find` | Find files or directories under `directory` matching `query`. Query `type` (`file` default), `limit` (1 to 200, default 50). |
| `GET /fs/read` | Return the file at `path` under `directory` as an inline download. |

The `fs` routes resolve `path` relative to `directory` and reject paths that
escape it, including through symlinks. Without `directory` they use the
daemon's working directory, which for an auto-started daemon is `~/.yoke`.
Always pass `directory`.

Errors: 404 `location_not_found`, 400 `location_path_required`,
400 `location_path_not_absolute`, 404 `location_parent_not_found`,
403 `location_parent_unreadable`, 403 `path_outside_location`,
404 `path_not_found`, 400 `not_a_directory`, 400 `not_a_file`,
403 `path_unreadable`.

## Uploads

| Method and path | Purpose |
| --- | --- |
| `POST /upload` | Multipart form with one `file` field. Query `sessionID` binds the upload to a session. Returns `id`, `uri`, `name`, `mime`, `size`, `expiresAt`. |

Limits and rules are in the guide under
[Upload images and files](http.md#upload-images-and-files).

Errors, from `/upload` or from admitting a prompt that references an upload:
413 `attachment_too_large`, 400 `unsupported_attachment`, 400 `invalid_image`,
400 `invalid_attachment_uri`, 404 `upload_not_found`,
403 `attachment_session_mismatch`, 409 `attachment_identity_conflict`.

## Error codes

All codes in one place, by status.

| Status | Codes |
| --- | --- |
| 400 | `empty_continuation`, `empty_mutation`, `empty_process_input`, `empty_prompt`, `empty_question_reply`, `invalid_archived`, `invalid_attachment_uri`, `invalid_continuation`, `invalid_cursor`, `invalid_cursor_anchor`, `invalid_image`, `invalid_permission_reply`, `invalid_pinned`, `invalid_process_signal`, `invalid_queue_operation`, `invalid_question_reply`, `location_path_not_absolute`, `location_path_required`, `mcp_config_update_failed`, `not_a_directory`, `not_a_file`, `session_startup_failed`, `title_regeneration_unavailable`, `too_many_attachments`, `unknown_tool`, `unsupported_attachment`, `workspace_unavailable` |
| 401 | `unauthorized` |
| 403 | `attachment_session_mismatch`, `location_parent_unreadable`, `path_outside_location`, `path_unreadable` |
| 404 | `daemon_mode_required`, `entry_not_found`, `location_not_found`, `location_parent_not_found`, `mcp_server_not_found`, `message_not_found`, `path_not_found`, `permission_not_found`, `process_not_found`, `queue_item_not_found`, `question_not_found`, `session_not_found`, `skill_not_found`, `tool_call_not_found`, `upload_not_found` |
| 409 | `attachment_identity_conflict`, `client_not_attached`, `daemon_busy`, `input_identity_conflict`, `model_context_too_small`, `process_input_failed`, `process_not_running`, `queue_item_not_editable`, `queue_revision_conflict`, `session_busy`, `session_has_pending_work`, `session_identity_conflict`, `session_workspace_busy`, `session_workspace_conflict`, `session_workspace_unavailable`, `tree_revision_conflict`, `tree_root_navigation_unsupported` |
| 413 | `attachment_too_large`, `prompt_too_large` |
| 422 | `validation_error` |
| 502 | `title_regeneration_failed` |
| 503 | `daemon_exiting`, `model_catalog_unavailable` |
| 507 | `selection_metadata_not_saved` |

## Maintaining the contract

The checked-in contract is `tests/yoke/http/golden/openapi.json`. After
changing the API, regenerate it and the TypeScript client types:

```bash
uv run python scripts/generate_http_openapi.py
cd clients/typescript && npm ci && npm run generate && npm run check
```

Commit the regenerated `openapi.json` and `clients/typescript/src/schema.d.ts`
together. The `HTTP contract` GitHub Actions workflow fails when either drifts.
