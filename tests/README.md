# Tests

Run the complete suite with `uv run pytest`. The default uses four xdist
workers. Daemon lifecycle, daemon controls and managed-process batches each stay on
one worker to bound simultaneous cold starts; short tests fill the other workers.
Use `-n 0` for serial debugging. Every test runs by default; there are no
separate opt-in integration or stress groups.

Each test gets its own HOME, runtime directory and session directory through
`conftest.py`. Subprocesses inherit that isolation. Each worker checks for
writes to the real database and detached daemons left by its own tests.
Use ephemeral TCP ports or temporary Unix sockets. Never use a running user
daemon as a fixture.

`yoke/fixture_server.py` keeps real HTTP and SSE transport while replacing
uvicorn's polling and fixed shutdown sleeps with an event. Close clients and
the event broker before stopping it. Application lifespan shutdown still
drains runtimes and closes storage resources. Pure request validation tests
should create a listener only when they reach daemon discovery.

The agent-run reporting fixture shortens its idle socket accept tick while
retaining peer credentials, capability authentication and frame handling.
HTTPX transports share default CA setup per worker and protocol configuration;
explicit TLS contexts, client certificates and certificate environment overrides
use their normal setup. Connections and pools remain independent. FastAPI fields reuse immutable
Pydantic adapters only when their full annotation, constraints, aliases,
defaults and config match. Dynamic annotations use normal construction;
applications, resolved dependency values and routes remain separate. Static
Yoke HTTP dependency descriptions are also shared; they resolve services from
each request's app state. A two-app test checks token and database isolation.

Non-storage tests copy an empty schema into separate database files. Migration,
storage, atomic-write and legacy-import tests still initialize databases through
the normal path, as do existing database files and actual CLI subprocesses.
Schema copies use independent inodes and atomic publication.

File-content, permission and UI projection fixtures run their file tools in
process. The core tools oracle and dedicated tool-worker tests keep the
real process boundary; all frozen provider bytes still match.

Prefer operation counters, barriers and events to large inputs or fixed
sleeps. The session performance fixture has 32 active entries and 224 inactive
entries; scan and copy assertions detect accidental full-history work without
requiring tens of thousands of rows. Queue concurrency deliberately forces a
revision conflict. Durability kills a writer after each kind of acknowledged
commit, then checks all acknowledged data and SQLite integrity.

Keep the provider projection oracles and frozen fixtures, socket and bearer
authorization, privacy diagnostics, schema migration, image reconstruction
and garbage collection, compactor recovery, branch sharing and verified
legacy import coverage. Keep one inexpensive test per public contract.
Additional transport or frontend combinations should cover a distinct code
path. Real subprocess tests are for process ownership, flock, restart,
authorization and entrypoint smoke coverage.

On hosts with a small `/tmp`, put temporary files under the home directory:

```sh
mkdir -p "$HOME/.yk-fast-work"
TMPDIR="$HOME/.yk-fast-work" uv run pytest --basetemp="$HOME/.yk-fast-work/pytest"
```

Remove that temporary directory after the run. Keep the repository's required
ruff, ty and pyright checks passing too.
