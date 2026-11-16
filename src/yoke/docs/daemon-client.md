# Daemon client contract

CLI command entry points use this contract. Daemon startup and runtime creation
share the saved-selection adapter in `cli/providers/state.py`; neither imports
CLI execution helpers. Basic and prompt-toolkit interactive
clients use the same daemon attachment and event stream as headless clients. The
HTTP API supports headless, basic and prompt-toolkit clients. Paths have the
`/api/v1` prefix.

## Startup and attachment

Create a session with `POST /session`. Set `startup: true` to run the existing CLI
startup resolver and receive a top-level `startup` object alongside `data`.
Supplying `skills: ["name", ...]` also requests startup resolution. The existing
web request remains valid and can create an empty session without credentials.

`selection.provider`, `selection.model`, and `selection.reasoningEffort` are CLI
flags. Model references can include the provider and effort. New-session
precedence is flags, project settings, user settings, then provider defaults.
An existing session supplies saved selection defaults through the same CLI
resume resolver. Explicit flags still win. `headless: true` preserves the CLI's
strict unknown-model behavior; interactive startup can recover an inherited
stale model and returns the CLI warning without rewriting settings files.

The startup object contains `warnings`, `activeSkills`, `modelSource`, and
`toolReport`. The report includes the CLI discovery message, active counts by
source, active and denied tool names, config path, and unmatched tool/capability
settings. Startup can run against an idle existing session without discarding
its conversation. Startup errors return `400 session_startup_failed`; the empty
session remains available for a corrected request.

Include `client` in creation, or `PUT /session/{id}/client` afterwards:

```json
{
  "owner": "terminal-unique-id",
  "kind": "cli",
  "env": {"PATH": "/usr/bin:/bin", "EXAMPLE_API_KEY": "..."},
  "cwd": "/absolute/existing/directory"
}
```

`kind` defaults to `acp`, preserving existing integrations' client tool and
approval behavior. `cli` and `web` attachments preserve the ordinary tool set;
they do not add ACP's question/plan tools or its instruction messages. CLI
clients should use `full-access` mode and keep local terminal interaction local.
MCP server injection supports `envVars` to explicitly inherit names from the
client environment, plus the existing explicit `env` values.

Attachments coexist by owner. Replacing an owner creates a new configuration
snapshot; admitted turns keep their original snapshot. `GET .../client?owner=`
selects an owner; the response includes attached `owners`. Omitting the owner
selects the most recently attached remaining client. `DELETE .../client?owner=`
removes only that owner and is safe during a turn. It neither interrupts work
nor changes another attachment's tools, approval policy, or environment.

## Admission and capacity

Add `clientID` to `POST /session/{id}/prompt` to select the admitting attachment.
An unknown explicit client returns `409 client_not_attached`. A prompt without
`clientID`, such as the browser's, uses the most recently attached remaining
client. After all clients detach, it retains the last attached environment
without the detached client's approval policy or injected tools. A session that
has never attached a client uses the daemon environment. Admission captures
ownership in memory before a competing request can promote that input. Queued
inputs retain their environment after detach or reconfiguration. An idempotent
replay retains the first admission's owner.

`session.prompt.admitted` includes `inputID`, `clientID`, `delivery`, and the
complete `prompt` with text, continuation flag and attachments. Every client on
the session receives the event. Use input IDs to avoid rendering a locally
submitted prompt twice. Promotion and settlement retain their existing events.

The default is 16 simultaneously executing sessions, with 20 worker threads and
a separate four-thread storage pool. Most model calls wait on network I/O;
16 supports several terminals and browser sessions without allowing unbounded
physical execution on the four-core host. Excess turns wait on the existing
FIFO semaphore. Admission succeeds, and `session.active.changed` plus
`GET /session/active` expose `activity: "Waiting for a slot"` while `state` stays
`running`. This preserves existing browser busy-state handling. Interrupt works
while waiting. Attached idle clients consume no execution slots.

## Event stream and renderer mapping

`GET /event?sessionID={id}&client={owner}` opens a filtered stream and a daemon
lifetime lease. Each stream owns its lease. Closing one stream leaves the other
clients' leases intact. Filtering happens before bounded queue admission, so a
busy unrelated session cannot force this stream to resynchronize. The stream
includes its session's durable and live events, `server.connected`, and global
resynchronization/shutdown notices. Without `sessionID`, the web feed remains
global. Unknown sessions return 404. Durable recovery still uses
`GET /session/{id}/history?after={seq}` and transcript reads.

The SSE `event` and envelope `type` use these names. Agent payload keys remain
in their original snake_case form. CLI clients translate the names back before
calling their existing renderer. Each live agent event carries `turnID` and
`inputID`, except existing context snapshots which already carry their IDs.

| Agent callback | SSE event | Renderer payload |
| --- | --- | --- |
| `model_start` | `session.model.started` | iteration, provider/model timing metadata |
| `model_end` | `session.model.ended` | iteration, usage, duration, provider/model metadata |
| `provider_retry` | `session.provider.retry` | provider, model, attempt, max_retries, delay_seconds, message |
| `provider_rate_limited` | `session.provider.rate_limited` | same retry telemetry |
| `provider_recovered` | `session.provider.recovered` | provider, model, attempts |
| `notice` | `session.notice` | original notice fields |
| `warning` | `session.warning` | original warning fields |
| `error` | `session.error` | original error fields |
| `assistant_message` | `session.message.updated` | content, phase and original message metadata |
| `tool_execution_start` | `session.tool.started` | tool_name, tool_call_id, tool_arguments, iteration |
| `tool_execution_output_delta` | `session.tool.delta` | tool_call_id, tool_name, delta/output cursor fields for the live inspector |
| `tool_execution_end` | `session.tool.ended` | tool_name, tool_call_id, ok, error, result/output, timing |
| `compaction_summary_start` | `session.compaction.summary.started` | estimated_input_tokens and original progress fields |
| `compaction_summary_end` | `session.compaction.summary.ended` | ok, duration_seconds, response_chars, error |
| `context_compaction` | `session.context.compacted` | original compaction counts, limits and reason |
| `compaction_start` | `session.compaction.started` | original callback fields |
| `compaction_progress` | `session.compaction.delta` | original callback fields |
| `compaction_end` | `session.compaction.ended` | original callback fields |
| `context_usage` | `session.context.updated` | original token/limit fields plus existing normalized snapshot |

The audit covers `cli/interactive/turn_renderer.py`, `renderer.py`, the basic
and headless renderers in `cli/render/status.py`, provider telemetry formatting,
and runtime turn callbacks. Numeric token accounting survives secret-key
redaction. `provider_result_projection` remains private. The web controller
refreshes only its recognized types and ignores the additional types.

## Environment and privacy

Environment dictionaries and attach cwd values remain in memory. They are not
session/admission fields and are never included in client status or events.
The session's project root remains its existing persisted workspace identity.
Command and Python processes use the admitting client's cwd and copied base
environment. Python interpreter selection reads that environment's virtualenv;
interpreter-specific shim directories avoid races between clients. MCP retains
its explicit inheritance policy, using the client environment for permitted
base names and `envVars`, then server-specific overrides.

Provider construction, catalog validation, model switching and title creation
use scoped environment resolution. Stored provider credentials still fill
missing values; explicit client values win and daemon process API keys are not
merged into an explicitly supplied environment. Provider state follows the
established fork/promote lifecycle within each client configuration. No provider
request message, tool-result ordering, cache scope or compaction handoff format
changes for a CLI attachment with an equivalent configuration.

Project `.env` loading reads into a copy. Explicit client values win over project
values. It does not inject `YOKE_HEADLESS`: that flag disables the CLI status
renderer and belongs to the client that chooses headless mode. An explicit
client value is preserved for tools and child Yoke processes. Dotenv loading
does not mutate `os.environ`.
Provider plugins and repository tools that directly inspect `os.environ` still
see the daemon process, as described in the approved design.

Transcripts are user content. Entries, messages, tool results and compaction
summaries are persisted without sanitation, preserving the provider's live
history and cache identity on resume. Message, tool and prompt events mirror
that content without substitution, so live rendering agrees with history.
This applies to durable events too. A tool that prints an environment value
has deliberately added it to the conversation.

Diagnostic sanitation registers only values of at least eight characters from
secret-looking environment names containing `key`, `token`, `secret`,
`password`, `credential` or `auth`, including known provider credential names.
Ordinary terminal values such as `SHLVL=1`, `COLUMNS=80`, paths, and nonsecret
sentinels remain intact. Retired and detached clients' secret values stay
registered in memory until shutdown to cover queued work and late diagnostics.
Sanitation applies to tool inspectors, provider debug files, API errors, startup
warnings, warning/error/retry events and diagnostic error fields on other
events. Existing structured secret-field sanitation in tool inspectors remains.
It never modifies the transcript or provider request objects.

The daemon configures its own root and uvicorn handler formatters, and sanitizes
its stdio path, including the detached rotating log. Formatters sanitize a copy
of the rendered record, including tracebacks. Attaching a client installs no
logging filters and never rewrites shared records or library handlers. Provider
file logging sanitizes before truncating diagnostic strings. API validation
errors omit request bodies, rejected input values and exception contexts, so an
invalid attachment cannot echo its environment or cwd. Client status responses
expose attachment metadata and owner IDs only.

An interrupted daemon loses private attachment/admission ownership. Recovered
work uses a newly attached client's environment or the daemon fallback. Clients
must reattach after restart; credentials are deliberately not recoverable from
SQLite. Phase 5 implements this reconnection behavior and terminal rendering.

The CLI's `SessionConnection` implements transport recovery. Each reader owns
its attachment and filtered SSE lease. After connecting or receiving
`server.resyncRequired`, it reads all newer history pages and the active
transcript before announcing `client.resynced`. Durable sequence numbers prevent
duplicate replay. A dropped stream repeats discovery, build-skew checks and
attachment with a new copy of the terminal environment. Ambiguous HTTP writes
are never retried automatically; admission retries must reuse their input ID.

CLI image attachments set `imageProjection: "cli"`. This preserves the existing
terminal provider message bytes: labels start at one per submitted prompt and
the image's `attachment_name` remains unset. Omitted projection keeps the web
and ACP behavior. The flag is durable, survives queue edits and recovery, and
does not supply arbitrary provider messages. Unsupported CLI images follow the
existing runtime image projection instead of the web admission rejection.

## Interactive completion and metadata

A terminal records its locally rendered input IDs before POST returns. Its
session observer skips those IDs and renders other clients' admitted prompts
and completed answers once. Queue snapshots are presentation state only; the
daemon promotes queued inputs. Re-admitting a stale UI copy would execute the
same turn twice, so the local dequeue path is disabled for daemon sessions.

`GET /session/{id}` includes read-only `databasePath` and
`contextWindowTokens`. `/info` refreshes that response and uses `tree.entryCount`
and `time.created`/`time.updated`, matching the in-process metadata display.
Tool and process inspectors convert validated response DTOs into their existing
terminal display values. Manual compaction waits for `/wait` before updating UI
history. Both resume modes use the existing `/relocate` route.

The event reader counts completed reattachments. After an uncertain admission,
the terminal can wait for a newer attachment before replaying the same input ID;
other mutations are never replayed automatically.

## Workspace ownership

The daemon owns shared execution leases and exclusive relocation leases in
memory, keyed by database and session ID. Admission, provider operations, title
work and background command cleanup retain their leases until physical work
stops. Clients use the relocation route; no workspace lock files are created.
ACP holds a daemon lifetime lease over either the discovered socket or its
explicit authenticated HTTP URL. Its former local ownership flock is gone.

Interactive resume replays the saved transcript before accepting another turn.
Startup retains saved history and avoids rewriting an unchanged selection and
skill snapshot. An interactive session whose saved provider was removed reports
the existing recovery warning and resolves an available provider. Explicit
selections and headless startup remain strict.
