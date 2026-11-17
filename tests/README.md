# Tests

Run the complete suite with `uv run pytest`. The default uses six xdist workers.
The daemon spawn race, real transport authorization and headless entrypoint
smoke share a worker to bound cold starts. Use `-n 0` for serial debugging.
Every remaining test runs by default. There are no opt-in integration groups.

Each test gets its own HOME, runtime directory and session directory through
`conftest.py`. Subprocesses inherit that isolation. Each worker checks for
writes to the real database and detached daemons left by its own tests.
Use ephemeral TCP ports or temporary Unix sockets. Never use a running user
daemon as a fixture.

The suite favors unit and service tests. Process/PTY integration coverage is
limited to the daemon auto-spawn/flock race, Unix peer versus TCP bearer auth,
one headless prompt, and one interactive PTY render smoke. The protected
provider oracle also executes real tools where its frozen production path
requires them. Process crash/restart, kill-9 durability, cross-process queue
concurrency, agent issuer death and most process-tool lifecycle coverage were
removed with owner approval. Do not infer coverage of those behaviors from the
remaining unit tests.

HTTP coverage retains representative auth, session, prompt admission, SSE,
upload and tool-trace contracts and the unchanged OpenAPI golden. Most repeated
HTTP runtime, attachment, recovery and load scenarios have been removed.
Read models, repositories, queue transactions, security/privacy and runtime
units remain. CLI tests retain distinct behavior checks; the repeated full
catalog/effort CLI round-trip matrix was dropped. Configuration and provider
units retain advertised effort validation.

`yoke/fixture_server.py` keeps real HTTP and SSE transport while replacing
uvicorn's polling and fixed shutdown sleeps with an event. Close clients and
the event broker before stopping it. Application lifespan shutdown still
drains runtimes and closes storage resources. HTTP contract TestClients enter
once per test; the two read-only OpenAPI tests share a module app/client.
Agent-run listener cleanup uses production socket shutdown without a test-only
accept-timeout patch.

Non-storage tests copy an empty schema into separate disposable database files
and explicitly select SQLite `synchronous=OFF`. Migration, storage, atomic-write
and legacy-import tests use normal initialization and `synchronous=NORMAL`, as
do actual CLI subprocesses. Schema copies use independent inodes and atomic
publication. WAL, transactions and rollback remain enabled.

File-content, permission and UI projection fixtures run file tools in process.
The tool stack imports only when one of those fixtures needs it. All 16 provider
oracle scenarios, their original driver choices, storage-wire goldens and the
runtime-request differential remain unchanged. Never regenerate frozen fixtures
as part of performance work. Migrations, image reconstruction and reachability,
in-process compactor resume/idempotence, fork sharing and verified legacy import
remain covered.

Prefer operation counters, barriers and events to large inputs or fixed sleeps.
For temporary files on this host:

```sh
mkdir -p "$HOME/.yk-fast3-work/tmp"
TMPDIR="$HOME/.yk-fast3-work/tmp" uv run pytest \
  --basetemp="$HOME/.yk-fast3-work/pytest"
```

Remove that temporary directory after validation. Run the repository's required
ruff, ty and pyright checks too.
