# Shared daemon and SQLite storage

Status: approved. Owner decisions are marked **(decided)**.

## Goals

1. The CLI, web UI and ACP attach to one per-user Yoke daemon. The first client starts it; the user never manages it.
2. Yoke persistence moves to one SQLite database. Credentials, settings and three file-shaped stores stay as files.

Provider-bound behavior must not change: message content and order, tool sequencing, compaction handoff, image projection and cache identity. Every phase proves this with a differential oracle.

## Decisions

| Topic | Decision |
|---|---|
| Tool environment | **(decided)** The attaching client sends its environment and working directory. Tools in that session use it. It is held in daemon memory only, never persisted, and redacted from events, traces and logs. |
| Credentials | Follows from the above: provider resolution for a session reads the client environment, not `os.environ`. A daemon under systemd has no shell API keys. |
| Lifecycle | **(decided)** Auto-spawned by the first client. It stays up while at least one client is connected or a turn is running or queued, then exits after a short grace period (~5 s). There is no idle timer and no private mode. `--no-idle-exit` keeps it always-on for systemd. Background processes started by tools stop with the daemon, as they stop with the CLI today. |
| Fallback | **(decided)** None. One runtime path: the daemon. |
| Custom runners | **(decided)** The `AgentRunner` protocol is dropped; the daemon runs `RuntimeAgent`. |
| DB scope | **(decided)** One global `~/.yoke/yoke.db`, with workspace as a column. Project `.yoke/` keeps only settings, tools and skills. |
| Existing sessions | **(decided)** Start fresh. Old JSONL is not read. A JSONL→SQLite importer is built as the QA harness and shipped as `yoke db import-legacy [id\|--all]`. |
| Stays as files | **(decided)** Credentials and auth (provider auth, codex-auth, MCP server OAuth), settings (`config.json`, `mcp.json`), SDK `state_path` snapshots, MCP full-output files and provider request debug logs. |
| SDK | Remains an in-process library with no daemon. Its only DB writes are usage rows. |
| Origin | A `sessions.origin` column (`cli`, `web`, `acp`, `sdk`) is added in the first schema. Hiding ACP sessions and settling them on disconnect is a later feature. |

## Daemon

**Transport.** One uvicorn server with two listeners:
- Unix socket `$XDG_RUNTIME_DIR/yoke/daemon.sock` (falls back to `~/.yoke/run/`), authorized by `SO_PEERCRED` uid. Used by the CLI, ACP and SDK children.
- TCP `127.0.0.1:8770` with a bearer token for the browser. If the port is taken, the daemon runs socket-only and reports it.
- `~/.yoke/daemon.json` (mode 0600) records pid, build id, socket path, port and token.

**Start and races.** The daemon holds an exclusive `flock` on `~/.yoke/daemon.lock` for its lifetime and binds only after taking it. A client connects to the socket; on failure it spawns a detached `yoke serve --daemon` and polls for up to 5 s. When two clients race, the losing daemon fails the lock and exits 0. Stale sockets and stale `daemon.json` are detected by the lock, not the pid.

**Leases.** A client holds a lease for as long as its event stream stays open. Exit condition: no leases, and no running or queued turns. A lease is per client, and a session may have several clients (CLI plus web), replacing today's single replaceable client session.

**Version skew.** `/health` reports a build id: version, git HEAD and dirty flag.
- Idle daemon, mismatched build: the client asks it to restart, then re-attaches.
- Busy, protocol-compatible: attach and print one warning. The daemon restarts once idle.
- Busy, incompatible: refuse. Never interrupt another client's turn.
- The daemon imports its runtime eagerly at start, so a `git pull` under a running daemon cannot load new modules into an old process.

**yoke-t3.service** becomes the daemon: `yoke serve --daemon --no-idle-exit`, `Restart=always`. ACP discovers it like any client. The flock in `acp/ownership.py` is replaced by a daemon lease.

**CLI as a client.** The agent loop emits whole messages, not token deltas, so the CLI renders the same events it receives over SSE.

| CLI feature | API | Work |
|---|---|---|
| Streaming turn | `POST prompt`, `GET /event` | Per-session event filter; publish the events the CLI renders (gap 2) |
| Steer and queue | `delivery`, `PATCH queue` | none |
| Esc-Esc stop | `POST interrupt` | none |
| Tool and process inspectors | `tool-call`, `process/output?afterSeq`, `signal` | none (polling is acceptable) |
| `/tree`, `/model`, `/mcp`, `/compact`, `/fork` | present | none |
| `/tools` | `PATCH session/tool` | Config-file writes stay client-side |
| `$EDITOR`, clipboard images | client-side; images via `POST /upload` | none |
| Headless | `prompt`, `wait`, `message` | Startup options (gap 3) |

API gaps to close before the CLI moves:
1. `/event` is one global stream. Add `?sessionID=`.
2. `_PUBLIC_AGENT_EVENTS` drops `model_start`, `model_end`, `provider_retry`, `provider_rate_limited`, `provider_recovered`, `notice`, `warning` and `error`. Publish them.
3. Session creation only takes session and root. Carry `--skill`, model source precedence, the startup warning and the tool-load report.
4. Several clients per session (leases), and the CLI renders prompts sent by other clients.
5. `max_active_sessions` becomes visible once every terminal attaches. Raise it, or make it a queue the CLI shows.
6. `YOKE_HEADLESS` and `.env` loading mutate `os.environ`. Make them per-session.

**Environment plumbing.** `ClientSessionRequest` gains `env` and `cwd`. They are held on the tool session context and threaded into `CommandProcessManager(base_environment=…)`, `python_env`, MCP subprocess environments and provider resolution. A turn uses the environment of the client that admitted its prompt. A web prompt falls back to the session's last attached client environment, then the daemon's own.

**Known limits.** Repo tools and provider plugins load into the daemon process and see its environment; they share `sys.modules` across workspaces. A daemon crash stops every client's turns. Both limits are documented, not solved, in this project.

## SQLite

**Connection model.** WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`. One writer thread owns one connection and runs `BEGIN IMMEDIATE` closures from a queue. Each storage-executor thread has its own read-only connection. SDK processes open their own connection only to insert usage rows.

**Schema sketch.**

```sql
sessions(id PRIMARY KEY, origin, workspace, title, pinned, archived_at,
         provider, model, effort, leaf_id, tree_rev, queue_rev, event_seq,
         context_usage, active_skills, created_at, updated_at, last_user_at)
  -- index (archived_at, updated_at), (workspace, updated_at)
entries(session_id, seq, id, parent_id, kind, role, call_id, created_at,
        message BLOB, metadata TEXT, PRIMARY KEY (session_id, seq))
  -- unique (session_id, id); index (session_id, parent_id), (session_id, call_id)
events(session_id, seq, id, type, time, data, PRIMARY KEY (session_id, seq))
inputs(session_id, id, state, delivery, paused, position, prompt,
       admitted_seq, promoted_seq, PRIMARY KEY (session_id, id))
uploads(id PRIMARY KEY, session_id, name, mime, sha256, expires_at)
blobs(sha256 PRIMARY KEY, data BLOB)
usage_events(event_id PRIMARY KEY, completed_at, provider, model, surface,
             session_id, input_tokens, output_tokens, cached_tokens, ...)
agent_runs(...), tool_traces as queries over entries.call_id
```

- `entries.message` holds the exact bytes today's entry codec writes, and is immutable. Mutable entry metadata (labels, summaries) lives in `metadata`, so updates no longer append delta lines. In the largest current session, 244 MB of 893 MB is such deltas.
- List queries never select `message`.
- Inline base64 images stay inline at first. Out-of-line image dedupe changes the stored form, so it is a later phase with its own differential test.

**Transactions replacing locks.**
- Entry append and session row update commit together. `index.json`, index repair and the periodic index maintenance go away.
- Queue and tree edits are compare-and-swap on `queue_rev` and `tree_rev`. A conflict returns 409.
- Admission and its durable event commit in one transaction (two files today).
- Workspace leases move into daemon memory.

**Maintenance.** `auto_vacuum=INCREMENTAL`, `incremental_vacuum` during idle, `VACUUM INTO` for `yoke db backup`. Migrations use `PRAGMA user_version` with ordered `.sql` files applied at daemon start. A binary that finds a newer schema version refuses to start.

**Search.** FTS5 over user and assistant text plus titles, after cutover.

**Deletable once SQLite is in.**
- `http/services/session_message_index/**` and the session read cache.
- `session/repository/{io,writer,index,index_cache,index_service,maintenance,load}` and the legacy decoders.
- The journal-offset logic in `session/events.py`, the file I/O in `session/admissions.py` and `session/queue.py`, and the usage-metric JSONL writer.
- Once the CLI is a client: `cli/runtime/{contracts,turn_agent,lifetime,workspaces}`, `execute_turn`, `cli/interactive/turn_runner.py`, `cli/interactive/queue/persistence.py` and `acp/ownership.py`.

## Phases

Each phase ships on its own and leaves the app working.

0. **Oracle.** Record provider request bodies and cache identity on a fixture provider across tools, skills, checkpoints, images, branches and compaction, using today's file store. Freeze work on the message-index tail scanners.
1. **Storage core.** Sessions, entries, events and inputs in SQLite behind the existing `SessionStore` interface. Ship the importer. The CLI and daemon still run as today and share the DB through WAL.
2. **Storage cleanup.** SQL pagination for reads; delete the index, sidecar and read cache; add uploads, usage and agent-run tables.
3. **Daemon plumbing.** Socket, lock, discovery, leases, build id, and `yoke daemon status|restart|stop`. ACP and yoke-t3 adopt discovery.
4. **API gaps.** Per-session events, published CLI events, startup options, multiple clients per session, per-session environment and credentials.
5. **CLI as client.** Headless first, then basic mode, then prompt-toolkit. Delete the in-process path in the same release.
6. **Follow-ups.** Origin filtering and ACP settle on disconnect, FTS, image dedupe.

## QA

| Check | Phases |
|---|---|
| Provider projection oracle: request bytes and cache identity identical (file vs SQLite, then in-process vs daemon) | 1, 2, 5 |
| Corpus: import all real sessions and compare every entry dump with the JSONL loader | 1 |
| Durability: `kill -9` a writer at random points; `integrity_check`, every acknowledged entry present, entries consistent with the session row; disk-full on a small tmpfs | 1, 2 |
| Concurrency: CLI and web on one session; CAS conflicts return 409; cross-client interrupt; slow SSE recovers through history; SDK usage inserts during turns | 2, 4, 5 |
| Spawn: 20 simultaneous `yoke --headless` start exactly one daemon; stale socket, stale lock, occupied port | 3 |
| Skew: two checkouts covering idle restart, busy warn, busy refuse, newer schema | 3 |
| Environment: two clients with different values of one variable run concurrently; a sentinel never appears in the DB or logs | 4 |
| Live smoke: CLI (pty), web (browser), ACP (JSON-RPC) against one daemon on one session | 5 |

Metrics recorded per phase: cold and warm attach time, time to first assistant event, resume time and RSS on the largest session, DB, WAL and freelist size, writer commit p99, SSE resync count.

## Phase 1 implementation notes

Choices made where the design was silent, and deliberate deviations.

- **Location.** `SessionStore()` and `SessionStore(<default session dir>)` use `YOKE_DB_PATH`, else `$YOKE_SESSION_DIR/yoke.db` when that variable is set, else `~/.yoke/yoke.db`. Any other directory passed to `SessionStore`, `AdmissionStore`, `SessionEventJournal` or the queue functions keeps its own `yoke.db` inside it, so tests and embedded stores stay isolated and those constructors are unchanged. The session directory still holds the files that are not in the database yet: workspace lease locks and uploads. `--session NAME` sessions were already global by id; they keep `workspace` as a column.
- **Connection model.** Instead of a writer thread, each process has one writer connection guarded by a re-entrant lock. `Database.write()` runs `BEGIN IMMEDIATE`; a nested `write()` on the owning thread joins through a `SAVEPOINT`, so a queue transaction that also appends an event, saves admissions and unarchives the session commits once. Reads use a per-thread `query_only` connection inside a deferred transaction; the thread holding the write transaction reads through the writer. `after_commit` callbacks run after the outermost commit; HTTP live events are published that way.
- **busy_timeout is 30 s, not 5 s.** A fork or import of the largest real session (7,564 entries, 650 MB of entry bytes) holds the write lock for about 8 s. With 5 s a concurrent CLI write would fail with "database is locked". Out-of-line blobs (later phase) make forks O(rows).
- **Schema additions.** `entries.message` holds the codec bytes of the whole `ConversationEntry` (the old JSONL `entry` object), so entries with no message still round-trip; `metadata` is NULL until an edit replaces it. Bookkeeping columns: `sessions.rev` (bumped by every write; read caches key on `(message_bytes, rev)`), `rewrite_rev` (an incremental read cannot span a full rewrite), `entry_seq` (sequence numbers are never reused), `entry_count`, `message_bytes`, and `entries.rev`. `queue_rev` and the CLI's pending image paths live in `session_queues`, because the CLI can persist a queue before its first save creates the session row. `inputs` keeps the existing `AdmissionRecord` and `PersistedPendingInput` payloads as JSON (`admission`, `pending`); the sketch's columns are derived from them after every write. Only `entries` has a foreign key; deleting a session deletes its events, inputs and queue explicitly.
- **Compare-and-swap.** Queue edits keep the existing revision checks, now inside one cross-process transaction. Tree edits (`save_tree_delta`, `save_entry_metadata` and the indexed variants) compare the stored `tree_rev` with the revision the caller's record was read at and raise `SessionTreeConflict`; HTTP maps it to `409 tree_revision_conflict`. Turn saves are not compared (unchanged semantics), but every save writes only the metadata columns that changed, so a title or pin set by another process is never overwritten.
- **Retention** (30 days, unpinned) still runs from `maintain_index`. Imported sessions keep their timestamps but carry a one-use hold, so an old import survives until its next conversation save or tree edit; normal retention then applies.
- **Reads.** `http/reads` read JSONL files directly, so the minimum adaptation was made: the message index builds topology from `entries` (tail scanners and sidecars deleted), the read cache catches up with `read_delta`, and tool traces and handoffs use store signatures and topology. SQL pagination and deleting the read cache remain Phase 2.
- **Importer.** `yoke db import-legacy` imports sessions only (not event journals, admissions or queues), honoring newer metadata in the legacy `index.json` exactly as the old loader did.
- **Workspace leases** remain `flock` files until the daemon owns them.

## Phase 2 implementation notes

- **Blobs and migration.** Schema v2 hashes the original v1 entry bytes with
  SHA-256 and moves them into `blobs`. `entries.message_sha256` references that
  table. Neither migration nor loading re-encodes the immutable payload. Entry
  metadata overrides remain separate. Fork copies rows and references only.
  Blobs use a rowid table with a separate unique key index: foreign-key checks
  must not walk large blob records. Migration is atomic and concurrent starters
  wait up to 60 seconds; ordinary writes use a 5-second timeout again.
- **HTTP reads.** Recursive SQL walks select active ancestry; message and tree
  cursors select entry IDs with SQL boundaries and limits. Session catalog
  filtering, ordering and pagination also run in SQL. Only selected payloads are
  decoded. Topology caches, background warming, paths and index provenance
  repair are gone. Pre-provenance tool images get a derived topology kind at
  insertion or migration, while loaders still return their original bytes.
  `SessionMessageIndex` remains a stateless query facade for runtime callers.
  `reads/cache.py` is only an import shim for the stateless runtime snapshot
  adapter. Explicit full-record projections retain their canonical fallback.
- **Uploads.** Metadata and original bytes are transactional DB rows, with bytes
  deduplicated in `blobs`. Phase 1 upload metadata files migrate when the upload
  service opens. Native file tools still need stable paths, which also occur in
  existing messages. Those files are disposable materializations rebuilt from
  the DB, including durable non-image attachments at startup. Upload bindings
  do not cascade on session deletion: a saved native file reference in a fork
  can still use the original attachment, matching file-store behavior. Expired
  unpinned uploads and explicitly removed queue attachments retain their existing
  cleanup policy.
- **Usage and agent runs.** SDK processes and the daemon insert privacy-safe
  metrics into `usage_events` through WAL. Reporting records keep schema 1,
  including missing-usage events and attribution. `read_usage_events` supports
  provider, session and timestamp filters. `YOKE_DB_PATH` selects the DB;
  `YOKE_USAGE_METRIC_LOG_DIR` no longer controls persistence. Usage rows do not
  cascade with sessions. The agent-run registry has no persisted file state;
  it remains memory-only. Credentials, settings, SDK state snapshots, MCP full
  output and provider request debug logs remain files.
- **Maintenance.** The existing 30-day unpinned retention and five-second
  maintenance cadence remain. Idle blob collection deletes up to 128 orphan
  blobs and reclaims up to 256 pages per pass. It skips a busy writer instead
  of waiting. Creation enables incremental auto-vacuum. Phase 1 databases
  already enabled it; externally created DBs with `auto_vacuum=NONE` require
  an offline `PRAGMA auto_vacuum=INCREMENTAL; VACUUM;`, documented in the CLI
  guide. Migration leaves freed v1 pages on the freelist; `yoke db backup`
  produces a compact copy without imposing a full vacuum during startup.

### Phase 2 validation

Implementation commit: `b4c22ad`. The originals in `~/.yoke/sessions` were only
read. The QA copy contained 443 JSONL files, totaling 1,249,136,237 bytes.

Both `yoke db import-legacy --all` runs, under Phase 1 code at `d218cfc` and
under v2, imported all 443 sessions with zero skipped, mismatched or failed
records. A v1 database built with that Phase 1 code then migrated to v2 in
29.974 seconds. The JSONL loader and importer verification compared every
record and all 17,216 entries again after migration. Integrity and foreign-key
checks passed. The corpus contains 17,204 distinct entry blobs, totaling
962,582,189 bytes.

The largest session was `20261025-135540-63d26b`, with 7,564 entries and
649,388,782 encoded entry bytes. Both forks requested metadata-only results,
so the measurements include copying the stored session and its entries, but
exclude parsing a returned full conversation.

| Measurement | v1 | v2 |
| --- | ---: | ---: |
| Fork wall time | 12.247 s | 0.355 s |
| Transaction body, BEGIN to COMMIT trace | 6.710 s | 0.258 s |
| Competing writer wait, observed lock interval | 7.139 s | 0.329 s |
| Explicit WAL checkpoint after benchmark cleanup | 0.104 s | 0.002 s |
| Compact DB size | 995,082,240 B | 986,505,216 B |

The contending writer begins after the entry-copy SQL starts and acquires
`BEGIN IMMEDIATE` as soon as SQLite's busy handler allows it. This includes
commit and retry/scheduling delay; the transaction-body timer excludes commit.
The remaining fork wall time includes connection/summary work and SQLite's
automatic checkpoint. A migrated DB occupies 1,976,291,328 bytes before idle
page reclamation, including 241,233 free pages; `VACUUM INTO` produces the compact
v2 size above. After fork/delete benchmarking, the directly imported v2 DB
occupies 991,367,168 bytes with 753 free pages.

A live `yoke serve --session-dir <temporary-directory>` run used the default
`codex` provider and `gpt-6.1-sol` model. It created a session, submitted a real
prompt, waited for idle, read two messages using one-message pages, forked,
and verified the fork's message JSON exactly matched the original. The provider
returned `SQLite smoke passed.`

Final checks: 2,277 tests passed and 4 skipped; `uv run ruff check .`,
`uv run ruff format .`, `uv run ty check` and `uv run pyright` passed. The full
suite includes the provider projection oracle and the full HTTP suite. Provider
projection fixtures, HTTP contract and context-equivalence tests, and the
OpenAPI golden are unchanged. No version bump or push was made.

### Storage QA follow-up

- Schema v3 adds an internal `import_hold` flag. A verified legacy import sets
  it in the import transaction. Retention ignores held rows. The next
  conversation save or tree edit clears it; ordinary sessions retain the
  existing 30-day policy. Displayed timestamps and pin state remain unchanged.
- Streaming entry iterators own separate read connections, so an iterator
  suspended on an executor thread cannot lend its old snapshot to subsequent
  tasks. Close or exhaust an iterator to release its WAL snapshot. Nested short
  reads still share one snapshot, and reads inside writes see uncommitted rows.
- Session and entry TEXT columns use the same surrogate normalization as the
  immutable JSON codec. Metadata edits require an existing session; only the
  initial provider metadata save explicitly permits creation.
- Catalog archive checks and metadata/events share the queue transaction.
  Relocation reads metadata under that transaction and materializes its full
  return value after releasing the database write lock. Upload file deletion
  runs after commit, so a queue rollback preserves native attachment paths.
- CLI turn and model saves catch SQLite errors alongside filesystem errors,
  preserving warnings and in-memory state. Database administration commands
  report unsupported schemas as clean CLI errors.
