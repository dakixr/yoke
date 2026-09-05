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

The `rg` and `fd` tools accept their native raw argument syntax. To keep their
MCP annotations truthfully read-only, subprocess-launching switches (`rg
--pre` and `fd --exec`/`--exec-batch`/`-x`/`-X`) are rejected; use
`exec_command` when command execution is intended.
MCP ripgrep also ignores `RIPGREP_CONFIG_PATH`, so a local configuration cannot
enable a subprocess hook behind the read-only argument check. The ordinary
agent `rg` tool retains its native configuration behavior.

The HTTP transport is stateless. One long-lived application runtime owns a
shared `CommandProcessManager`, so commands that outlive their initial call can
return an ephemeral process `session_id`. A later `process_io` call from any
MCP client connected to that runtime can poll the process or write stdin. Live
processes and handles are never persisted and are terminated when the service
stops. Run one ASGI worker unless process ownership is moved to a separate
executor.

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

The MCP adapter logs tool name, duration, and success status only. It does not
log tool arguments or file contents. Uvicorn access logging is disabled so
OAuth query parameters are not copied into ordinary HTTP access logs.

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
