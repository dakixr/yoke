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
- exactly eight tools: `read_file`, `rg`, `fd`, `skill`, `apply_patch`,
  `exec_command`, `exec_python`, and `process_io`

The `rg` and `fd` tools accept their native raw argument syntax. To keep their
MCP annotations truthfully read-only, subprocess-launching switches (`rg
--pre` and `fd --exec`/`--exec-batch`/`-x`/`-X`) are rejected; use
`exec_command` when command execution is intended.

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
| `--log-level` | `YOKE_MCP_LOG_LEVEL` | `info` |

`YOKE_MCP_ALLOWED_HOSTS` is a comma-separated list of accepted HTTP Host
headers. Add the public hostname when a reverse proxy or tunnel forwards an
external hostname to the loopback service.

`--skill-dir` may be repeated. `YOKE_MCP_SKILL_DIRS` uses the platform path
separator (`:` on Linux and macOS). The MCP-only `skill` tool recursively
discovers `SKILL.md` files in those directories, returns the full instructions
and absolute paths for every file in a requested skill directory, and does not
register itself with Yoke's agent CLI. Configured directories take precedence
over duplicate skill names; Yoke's built-in skills remain available as
fallbacks.

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

The OAuth provider intentionally accepts HTTPS redirect URIs only for hosts in
`YOKE_MCP_OAUTH_ALLOWED_REDIRECT_HOSTS` (plus HTTP localhost callbacks for MCP
Inspector testing). The default is `chatgpt.com`.

## Filesystem and command security

The configured root is a default context, not a sandbox. Relative paths resolve
from it; absolute paths and `..` remain valid when the operating-system account
can access them. Run the service as a dedicated account whose filesystem and
sudo permissions match the intended operations.

Child commands receive a small environment containing ordinary process
settings such as `PATH`, `HOME`, locale, shell, and temporary-directory values.
The MCP service's bearer token and unrelated service secrets are not inherited.
Additional variables can be named explicitly with the comma-separated
`YOKE_MCP_COMMAND_ENV_ALLOWLIST` variable. Keep tunnel credentials in a
separate service regardless.

MCP `exec_command` calls use a non-login shell by default so personal startup
files do not silently repopulate the filtered environment. A caller can still
request Yoke's explicit `login=true` behavior, and arbitrary commands can read
anything the service account itself can read. Treat the selected OS account as
the real permission boundary.

The server does not use a command denylist. OS permissions, narrow sudo rules,
network policy, authentication, and MCP action confirmations are the security
boundaries.

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
