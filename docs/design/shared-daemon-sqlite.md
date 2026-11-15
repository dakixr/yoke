# Shared daemon and SQLite storage

Status: proposed, awaiting approval. Owner decisions are marked **(decided)**.

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
