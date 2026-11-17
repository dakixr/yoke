# Sessions

Every conversation is a session. The shared daemon saves sessions in one SQLite
database, `~/.yoke/yoke.db`, so you can stop, resume, branch and hand off work.
This page covers those tasks and the `yoke db` maintenance commands.

## Where sessions live

| Item | Location |
| --- | --- |
| Session database | `~/.yoke/yoke.db` |
| Disposable upload files | `~/.yoke/sessions/` |
| Migration backup | `yoke.db.pre-v<N>`, next to the database |

Set `YOKE_DB_PATH` to use another database file. `YOKE_SESSION_DIR` moves both
the upload directory and the database, which becomes `yoke.db` inside that
directory. `YOKE_DB_PATH` wins when both are set. The daemon reads these
variables when it starts, so set them before the first `yoke` command or restart
the daemon after changing them.

The database holds conversation entries, branches, titles, pins, queued prompts,
uploads and usage records. Settings and credentials stay in their own files.
See [Configuration](configuration.md#file-locations).

Yoke deletes unpinned sessions 30 days after their last activity. Pinned
sessions are kept until you unpin them. Usage records are not deleted with
their session.

A session ID such as `20261117-143022-a1b2c3` is the UTC start time followed
by six random hex digits.

## Resume a session

```bash
# Choose from this workspace's sessions
yoke resume

# Resume one session by ID
yoke resume 20261117-143022-a1b2c3

# Resume with a different model
yoke resume 20261117-143022-a1b2c3 --model zai:glm-5.3-flash

# Run one headless turn in a saved session
yoke resume 20261117-143022-a1b2c3 -p "summarize what changed"
```

Without an ID, `yoke resume` prints a numbered list of up to 50 sessions whose
workspace is the current directory, or the directory given by `--root`, and asks
for a number. `--root` only filters this list. It never moves a session.
`yoke resume --all` lists the latest sessions from every workspace instead.

A resumed session runs in its saved workspace, whatever your current directory
is. Yoke prints the saved history, then waits for your next prompt.

## Name a session

`--session NAME` uses `NAME` as the session ID. The first run creates the
session and later runs continue it.

```bash
yoke --session auth-refactor "let's keep working on the auth module"
```

An existing named session runs in its saved workspace.

## Fork a session

A fork copies a session into a new ID and leaves the source untouched.

```bash
# Fork from the command line
yoke --fork 20261117-143022-a1b2c3

# Fork into another existing workspace
yoke --root /new/path/to/project --fork 20261117-143022-a1b2c3
```

Run `/fork` inside a session to copy it and continue in the copy. `--fork` and
`--session` cannot be used together, because one names the source and the other
names the target.

Yoke checks an explicit `--model` before it creates the fork. A bad selection
fails without creating anything.

## Navigate the session tree

A session is a tree. Each time you rewind and send a different prompt, Yoke
starts a new branch and keeps the old one. Future turns use only the active
branch.

Run `/tree` or press `Ctrl+X` then `T` to open the tree navigator. It is
available in full-screen mode only.

- Selecting a user message rewinds to just before it and puts its text back in
  the editor. Send it, edited or not, to start a new branch.
- Selecting an assistant, tool, compaction or summary entry continues from
  after that entry.
- Type to search. Inactive branches start folded, and search opens them.
- You can label entries. Yoke stores labels as metadata.

Before moving to another branch, Yoke asks whether to summarize the branch you
are leaving. `No summary` is the default. If you choose a summary, you can add
your own instructions to the summary prompt. After the move, Yoke reprints the
active branch so the scrollback matches.

If another client changed the tree since your session loaded it, Yoke rejects
the tree edit instead of overwriting the other change.

## Titles, pins and session info

Yoke generates a title from the first prompt in the background. The turn does
not wait for it. If generation fails, the session keeps a title taken from the
first prompt.

| Command | Action |
| --- | --- |
| `/title <new-title>` | Set the title. |
| `/regenerate-title` | Ask the model for a new title from the conversation. |
| `/pin` | Pin or unpin. Pinned sessions are not deleted by retention. |
| `/info` | Show the ID, title, pin state, database path, model, message counts, timestamps and context window. |
| `/new` | Leave this session and start a new one in the same workspace. |

## Recover a missing workspace

Each session remembers its workspace directory. Before a resumed session starts
work, the directory must exist and be readable. This applies to
`yoke resume ID` and to an existing `--session ID`. If the directory is gone,
Yoke stops with an error and suggests the fix. It does not create the
directory, fall back to your current directory, or pick another project.

You have three options:

1. Restore the directory at its old path and resume as usual.
2. Bind the session to a new directory with `--relocate`:

   ```bash
   yoke resume 20261117-143022-a1b2c3 --relocate /new/path/to/project
   ```

3. Fork into an existing directory with `yoke --root DIR --fork ID`. This
   creates a new session and leaves the source as it was.

Relocation keeps the session ID, history and branches. It does not move any
project files. Relocating to the current directory does nothing. An invalid
destination leaves the session unchanged. Sessions saved with no workspace at
all also need `--relocate`.

The daemon refuses to relocate a session that has a running turn, operation or
child process, or any queued prompt, including paused ones. Stop the work and
clear the queue first.

If the directory disappears after a prompt has left the queue but before it
runs, Yoke puts that prompt back in the queue as paused, with its images.
Restoring the directory does not run it. Unpause it in `/queue`, or delete it
before relocating.

After relocation, the next turn loads tools, skills, MCP servers and config from
the new directory. Skill instructions already in the conversation stay as they
were.

You can read a session without its workspace. Open it in the web UI, or print a
handoff as described next.

## Hand off a session

`yoke session-handoff` prints the active branch of a saved session as Markdown
that another agent can read. It reads the database directly and does not need
the daemon or an HTTP token.

```bash
yoke session-handoff 20261117-143022-a1b2c3

# Only the last three user turns and the work they caused
yoke session-handoff 20261117-143022-a1b2c3 --tail 3

# Full tool arguments and results
yoke session-handoff 20261117-143022-a1b2c3 --tool-detail full

# JSON for scripts
yoke session-handoff 20261117-143022-a1b2c3 --format json
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--format` | `markdown` | `markdown` or `json`. |
| `--tail N` | all | Keep the last `N` user turns and their assistant and tool messages. |
| `--tool-detail` | `compact` | `compact` keeps the start and end of each tool result. `full` keeps them whole. |
| `--max-chars N` | 64,000 | Approximate size limit, from 10,000 to 2,000,000. Applies to `full` too. |

The handoff starts with the title, workspace, saved model and effort, and active
skills. If Yoke compacted the session, the handoff uses the compaction summary
in place of the messages it replaced. The summary stays even with a short
`--tail`. Images appear as labels or local paths.

The built-in `yoke-sessions` skill uses handoffs to read saved sessions and to
find their workspace and model before continuing them.

## Maintain the database

```bash
yoke db info                       # path, schema version, sizes and row counts
yoke db check                      # read-only integrity check
yoke db migrate                    # create or upgrade the schema, with the daemon stopped
yoke db stats                      # table sizes and entry field sizes
yoke db compact                    # finish image compaction, collect garbage, VACUUM
yoke db backup ~/yoke-backup.db    # consistent copy with VACUUM INTO
```

| Command | Notes |
| --- | --- |
| `yoke db info` | Reads only. Reports an older schema and tells you to upgrade instead of changing it. |
| `yoke db migrate` | Creates the database or upgrades its schema. Refuses while a daemon is running, and holds the daemon's lock until it finishes. |
| `yoke db check` | Opens the database read only. Runs SQLite `quick_check` and foreign key checks, decodes a sample of 32 stored blobs and looks for orphan references. Exits 1 on any problem or on a schema version this release cannot read. Never creates or upgrades a database. |
| `yoke db stats` | Reads only. Shows table and index pages, plus logical entry bytes split into images, usage and other JSON. Logical totals count shared entries once per reference. Damaged entries are counted as unreadable and skipped. |
| `yoke db compact` | Shrinks old inline images in quiet sessions, deletes unreferenced blobs and runs `VACUUM`. Skips sessions that are active or were used in the last 10 minutes. Prints sizes before and after. Needs a database at the current schema. |
| `yoke db backup PATH` | Fails if `PATH` exists. |

The daemon does the same work in the background. It periodically deletes
expired sessions and unreferenced blobs, and compacts old images in small
batches while sessions are idle. Each background pass has a short time budget,
so a session with many large images may need `yoke db compact` to finish.

Compaction records an entry it cannot decode, for example a corrupt or missing
image blob, once and then skips it. The damaged data stays in place for
diagnosis. Reading that entry later fails with an error that names the
session, the entry and the image.

### Upgrades and backups

Only two things upgrade the schema: the daemon when it starts, and
`yoke db migrate` while no daemon runs. The CLI and the read-only `yoke db`
commands never change it, so a newer checkout cannot upgrade the database under
an older running daemon. Before an upgrade, Yoke writes a consistent copy
to `yoke.db.pre-v<N>` next to the database, where `N` is the new schema version.
It keeps only the latest such backup. If the backup fails, the upgrade does not
start. The SDK and `yoke session-handoff` never upgrade the database.

Yoke refuses to open a database written by a newer release. Upgrade Yoke to
read it.

### When the database will not open

The daemon log and the CLI error name the database path and the cause.

If the schema is newer than this release, upgrade Yoke. If the database is
locked, stop the other process that holds the write lock.

If the database is damaged, run `yoke db check` to see the problems. Then stop
every client, stop the daemon with `yoke daemon stop`, copy the damaged
`yoke.db` and its `-wal` file somewhere safe, and restore the latest
`yoke.db.pre-v<N>` backup.

### Reclaiming space in an old database

New databases use incremental auto-vacuum, so deleted data returns to the file
system over time. A database created without it needs a one-time offline
conversion. Stop every Yoke process, then run:

```bash
sqlite3 ~/.yoke/yoke.db 'PRAGMA auto_vacuum=INCREMENTAL; VACUUM;'
```

### Import sessions from older releases

Releases before the SQLite store saved each session as a JSONL file under
`~/.yoke/sessions/`. Yoke does not read those files. Import them:

```bash
yoke db import-legacy --all
yoke db import-legacy 20240421-143022-abc1 --from /path/to/sessions
```

The importer reads each file, writes it in one transaction, reads it back and
compares every entry. On a mismatch the importer rolls that session back and
reports it, and the command exits 1. Rerunning skips sessions already imported, so an
interrupted import picks up where it stopped. Imported sessions keep their
timestamps, titles and pins. An imported session is safe from retention until
you next use it. After that, the normal 30-day rule applies. The importer does
not bring over HTTP event journals or queued prompts.
