# Tool-only MCP server

`yoke-mcp` exposes a compact remote coding harness over MCP Streamable HTTP.
It reuses Yoke's local tools without instantiating a Yoke agent, provider,
conversation, session tree, compaction flow, or persistence layer. The MCP
client remains responsible for reasoning and conversation state.

## Start locally

```sh
yoke-mcp --root /srv/projects/my-app --host 127.0.0.1 --port 8765
```

The service exposes:

- `GET /healthz`
- MCP Streamable HTTP at `POST /mcp`
- the original eleven tools: `read_file`, `view_image`, `rg`, `fd`, `skill`, `apply_patch`,
  `exec_command`, `exec_python`, `process_io`, `mcp_inspect`, and `mcp_call`

The server also exposes `batch_read`, `result_read`, `process_read`,
`process_cancel`, `search_then_read`, `workspace_snapshot`, `check_patch`,
`import_files`, `write_binary_file`, and `export_file`, for 21 default tools.
`exec_python` includes a parent-owned tool-composition bridge. Explicitly
configured downstream wrappers may add reviewed names. See
[Composed MCP work](mcp-composition.md) for schemas, limits, recipes, file
transfer, and the single-user ownership contract.

`view_image` accepts a local PNG, JPEG, GIF, or WebP path and returns native MCP
image content while preserving the source bytes. Relative and absolute paths
follow the same rules as the other file tools. Invalid, unsupported, oversized,
or excessively large decoded images are rejected before any image is returned.

`mcp_inspect` and `mcp_call` let the authenticated remote client inspect and
call MCP servers configured through Yoke without starting a Yoke agent.

The `rg` and `fd` tools expose typed search semantics instead of native argument
strings. `rg` accepts pattern lists, paths, globs, file types, match/result
modes, context, ignore behavior, sorting, and a global result limit. `fd`
accepts a pattern, search paths, file types, extensions, excludes, depth and
time/size filters, ignore behavior, sorting, a global result limit, and an
optional regex over returned paths. Shell pipelines and subprocess-launching
switches are intentionally not representable; use `exec_command` when command
execution or arbitrary shell composition is intended. MCP ripgrep ignores
`RIPGREP_CONFIG_PATH`, while the ordinary agent `rg` tool retains its native
configuration behavior.

`rg` returns mode-specific structured output: match/context objects with paths,
line numbers and submatches, path lists for file modes, or `{path, count}`
objects for count mode. `fd` returns bounded path strings, or structured
`path`/`type`/`size_bytes`/`modified_at` metadata when `details=true`. `limit`
replaces common `| head` usage without shell parsing.

The typed contract is the only `rg`/`fd` API. Native argument strings and
execution switches are not accepted; use `exec_command` for arbitrary native
CLI behavior or shell composition.

The HTTP transport is stateless. One long-lived application runtime owns a
shared `CommandProcessManager`, so commands that outlive their initial call can
return an ephemeral process `session_id`. Long waits continue with bounded
`process_read` calls; `process_io` is primarily for terminal input and short
interaction. The OS process keeps running between MCP calls. Live
processes and handles are never persisted and are terminated when the service
stops. Run one ASGI worker unless process ownership is moved to a separate
executor.

## Command arguments and error recovery

Use `exec_command` with exactly one execution mode:

```json
{"cmd":"pwd"}
```

```json
{"argv":["git","status","--short"],"workdir":"/srv/projects/my-app"}
```

`cmd` is shell text, never an array. `argv` is a non-empty array of non-empty
strings and bypasses shell parsing. `command` remains a deprecated alias for
`cmd`. Supplying both aliases, both modes, or an unknown argument such as
`timeout` returns `INVALID_ARGUMENT`; it no longer silently ignores a typo.
The MCP descriptor is a plain object with named fields, without a root-level
union. Cross-field rules, including exactly one non-null command mode, are
checked by the runtime. The ordinary agent command schema is unchanged.

Argument errors return `stage: "input_validation"`, `execution_started: false`,
a `request_id`, field-level details without input values, and a corrective
example. Correct the call and retry the intended authorized operation. Do not
translate an argument error into a permission denial or claim a command was
attempted when it never started. Arrays are never silently joined into shell
text. A client may reject an invalid call before sending it to the server;
such a rejection can only be found in that client's transcript.

Execution failures are different. `COMMAND_EXIT_NONZERO` retains the command's
exit code; `COMMAND_TIMEOUT` and `COMMAND_CANCELLED` describe managed process
outcomes. A caught OS `PermissionError` is `OS_PERMISSION_DENIED`. Other errors
use `TOOL_ERROR` or `TOOL_EXECUTION_ERROR`, not a guessed permission reason.
`execution_started: null` means dispatch occurred but the adapter cannot prove
whether a process started or partial work happened. Inspect the result before
retrying. Result-encoding failures use `INVALID_TOOL_RESULT`. Never treat an
error after dispatch as proof that retrying cannot duplicate work.

Every reply also carries `yoke/request_id` and `yoke/version` in MCP `_meta`,
including image replies, without changing successful structured payloads.

## Configuration

CLI flags have environment equivalents:

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--root` | `YOKE_MCP_ROOT` | current directory |
| `--host` | `YOKE_MCP_HOST` | `127.0.0.1` |
| `--port` | `YOKE_MCP_PORT` | `8765` |
| `--default-yield-ms` | `YOKE_MCP_DEFAULT_YIELD_MS` | `30000` |
| `--python-timeout` | `YOKE_MCP_PYTHON_TIMEOUT` | `180` |
| `--max-output-tokens` | `YOKE_MCP_MAX_OUTPUT_TOKENS` | `20000` |
| `--max-remote-wait-ms` | `YOKE_MCP_MAX_REMOTE_WAIT_MS` | `240000` |
| `--allowed-host` | `YOKE_MCP_ALLOWED_HOSTS` | loopback hosts |
| `--skill-dir` | `YOKE_MCP_SKILL_DIRS` | built-in skills only |
| `--json-response` / `--no-json-response` | `YOKE_MCP_JSON_RESPONSE` | `false` |
| `--log-level` | `YOKE_MCP_LOG_LEVEL` | `info` |

`YOKE_MCP_ALLOWED_HOSTS` is a comma-separated list of accepted HTTP Host
headers. Add the public hostname when a reverse proxy or tunnel forwards an
external hostname to the loopback service.

Streamable HTTP uses SSE responses by default. The MCP SDK sends periodic SSE
keepalives while a long tool call is still running, which prevents idle-read
timeouts in reverse proxies from cutting off the request. Set
`YOKE_MCP_JSON_RESPONSE=true` or pass `--json-response` only for clients that
require one buffered JSON response.

Remote execution and observation calls are capped at 240 seconds by default so
they return before common five-minute connector deadlines. If a command is
still running, the result includes `continue: true`, `next_tool:
"process_read"`, and `recommended_wait_ms`. Calling `process_read` again with
its returned cursor continues observing the same OS process; it does not rerun
the command. The environment setting may lower this safety cap but cannot raise
it above 240 seconds.

`--skill-dir` may be repeated. `YOKE_MCP_SKILL_DIRS` uses the platform path
separator (`:` on Linux and macOS). The MCP-only `skill` tool recursively
discovers `SKILL.md` files in those directories, returns the full instructions
and absolute paths for every file in a requested skill directory, and does not
register itself with Yoke's agent CLI. Configured directories take precedence
over duplicate skill names; Yoke's built-in skills remain available as
fallbacks. Discovery runs on every `skill` call, so newly installed, updated,
renamed, or removed skills are visible without restarting the MCP service.
Temporarily invalid `SKILL.md` files are ignored so an in-progress installation
does not break access to the rest of the catalog.

## Downstream MCP gateway

`yoke-mcp` always loads the normal Yoke MCP configuration from
`~/.yoke/mcp.json` and `<root>/.yoke/mcp.json`. Workspace entries override
global entries with the same server name.

`mcp_inspect` returns compact metadata for configured servers and their allowed
tools. `mcp_call` invokes one selected tool. The gateway respects each server's
`enabled`, `enabled_tools`, and `disabled_tools` settings. Calls to the same
downstream server are serialized because stdio and stateful HTTP MCP sessions
share client state. Calls to different downstream servers can run in parallel.

Both gateway tools re-read the global and workspace MCP config before each
operation. Adding, changing, disabling, or removing a server therefore takes
effect without restarting `yoke-mcp`. Unchanged server clients stay connected.
When a server config changes, Yoke waits for any active call to finish, closes
that server's old client, and creates a new client on demand. Removed and
disabled servers are closed as part of the same reconciliation.

If a config file is temporarily invalid while it is being edited, the gateway
returns a reload error and keeps the last valid config and clients intact. The
next call retries the config load, so fixing the file recovers without a
service restart.

The outer Yoke OAuth flow authenticates ChatGPT or another remote MCP client to
Yoke. It does not authenticate Yoke to downstream MCP servers. Downstream stdio
servers use their configured environment, and downstream Streamable HTTP
servers can use headers configured in `.yoke/mcp.json`. Yoke does not currently
run an interactive downstream OAuth authorization-code flow.

`mcp_call` is intentionally advertised as a mutating, destructive, open-world
tool because one generic call can reach downstream read or write actions. Use
`enabled_tools` allowlists for services where the remote client should only
reach a subset of actions.

The ChatGPT-facing gateway preserves text and structured data, returns complete
selected schemas, and supports schema-pinned dispatch, pagination, and bounded
result handles. Validated downstream images are returned as native MCP image
blocks. Agent-side MCP projection remains unchanged. See
[discovery and results](mcp-composition.md#discovery-results-and-media) for the
server-specific output contract and retention limits.

Set `YOKE_MCP_BEARER_TOKEN` to protect `/mcp` with a static bearer token during
private deployment tests. The health endpoint remains public. Static bearer
authentication is not an MCP OAuth implementation and cannot replace OAuth 2.1
for a published ChatGPT app. A public deployment must use MCP-compliant OAuth
or another supported private connection mechanism.

For a private, single-user ChatGPT connection, enable the built-in OAuth 2.1
authorization-code flow with PKCE and Dynamic Client Registration:

```sh
YOKE_MCP_OAUTH_ISSUER_URL=https://mcp.example.com
YOKE_MCP_OAUTH_AUTHORIZATION_PASSWORD='use-a-long-random-secret'
YOKE_MCP_OAUTH_STATE_FILE=~/.local/state/yoke-mcp/oauth.json
YOKE_MCP_OAUTH_ALLOWED_REDIRECT_HOSTS=chatgpt.com
```

The issuer URL has no `/mcp` suffix. OAuth metadata, protected-resource
metadata, registration, authorization, token, and consent routes are mounted
automatically. The state file persists registered clients, access tokens, and
rotating refresh tokens across service restarts and is written with mode
`0600`. The authorization password is never exposed to child commands. Keep it
in the private service environment and enter it only on the server-hosted
consent page reached during the ChatGPT OAuth flow.

Keep the OAuth state file outside the source checkout and never commit it. It
contains generated client identifiers and bearer credentials. Yoke uses a
generic token subject and normalizes legacy token subjects when loading older
state files so local account names are not retained in OAuth metadata.

The OAuth provider intentionally accepts HTTPS redirect URIs only for hosts in
`YOKE_MCP_OAUTH_ALLOWED_REDIRECT_HOSTS` (plus HTTP localhost callbacks for MCP
Inspector testing). The default is `chatgpt.com`.

## Filesystem and command security

The configured root is a default context, not a sandbox. Relative paths resolve
from it; absolute paths and `..` remain valid when the operating-system account
can access them. Run the service as a dedicated account whose filesystem and
sudo permissions match the intended operations.

Child commands inherit the MCP service process environment so developer tools
see the same provider credentials, API tokens, runtime settings, and `PATH`
that were available when `yoke-mcp` started. Variables whose names begin with
`YOKE_MCP_` are removed before child processes start so the remote server's
bearer token, OAuth settings, and other MCP control values are not propagated.

On POSIX systems, `yoke-mcp` imports non-`YOKE_MCP_` variables from the user's
login shell once at startup. This makes a service-manager launch behave like a
normal terminal session without sourcing shell startup files for every tool
call. MCP `exec_command` calls still use a non-login shell by default. A caller
can request Yoke's `login=true` behavior for an individual shell command, or
pass `argv` instead of `cmd` to launch a process without shell parsing. Treat
the selected OS account as the real permission boundary.

The server does not use a command denylist. OS permissions, narrow sudo rules,
network policy, authentication, and MCP action confirmations are the security
boundaries.

The MCP entry point writes JSON logs. Each received tool request has paired
`tool_call_started` and `tool_call_finished` events, including early validation
errors, unknown tools, exceptions, and cancellations. Events include request ID,
tool name, Yoke version, server PID, stage, outcome, execution-start evidence,
error code, and duration. A running command's first request ends with the
`running` outcome; its process handle continues through `process_read`.

Call logs include argument count and the types of `cmd`, `argv`, and `command`,
never their values. Unknown tool names are redacted. The formatter does not
render exception strings or tracebacks. Command bodies, outputs, environment
values, tokens, and file contents are not copied into call logs. Uvicorn access
logging remains disabled so OAuth query parameters stay out of access logs.

## Updating a connected deployment

Build and test a pinned release, retain the previous release for rollback, and
check for live managed commands before restarting. A restart terminates live
commands and loses their process handles. Do not restart over an active
validation job merely to update metadata.

After deployment, verify health and authenticated `initialize`, `tools/list`,
and `tools/call` through the actual endpoint. Check the returned Yoke version,
command schema, invalid-argument recovery, successful command execution, and
paired journal events. Health alone does not prove the command contract works.

For a developer-mode ChatGPT connection, open the connection in ChatGPT Plugins,
select Refresh, confirm the tool metadata changed, and test in a new
conversation. A server-side update does not guarantee that an existing chat's
cached descriptor was refreshed. Published plugins instead require their
reviewed metadata snapshot to be updated. See the official
[connection testing instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata).

## Example systemd topology

Run the MCP and tunnel clients as separate services:

```ini
[Service]
User=yoke-mcp
Group=yoke-mcp
WorkingDirectory=/opt/yoke
EnvironmentFile=/etc/yoke-mcp.env
ExecStart=/opt/yoke/.venv/bin/yoke-mcp --root /srv/my-app
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
```

Bind `yoke-mcp` to loopback. Configure the separate tunnel or reverse proxy to
forward only the intended public hostname to `http://127.0.0.1:8765`, and keep
its credentials out of `/etc/yoke-mcp.env`.
