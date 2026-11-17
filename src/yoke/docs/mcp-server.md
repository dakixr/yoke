# Tool-only MCP server

This page is for people who connect ChatGPT or another remote MCP client to a
machine through `yoke-mcp`. It covers starting and configuring the server, the
tools it exposes, command errors, the downstream MCP gateway, authentication,
security and deployment. [mcp-composition.md](mcp-composition.md) covers the
batch, Python, recipe and file-transfer tools in depth.

`yoke-mcp` serves Yoke's local tools over MCP Streamable HTTP. It does not
start a Yoke agent, provider or conversation, and it saves nothing. The MCP
client does the reasoning and keeps the conversation. It is separate from the
per-user daemon in [http.md](http.md).

## Start the server

```sh
yoke-mcp --root /srv/projects/my-app --host 127.0.0.1 --port 8765
```

The server has two routes:

- `GET /healthz` returns `{"ok": true}` without authentication.
- `POST /mcp` is the MCP Streamable HTTP endpoint.

Run one ASGI worker. The server keeps live processes and result handles in its
own memory, and a second worker would not see them.

## Tools

The server exposes 22 tools by default:

| Tool | Purpose |
| --- | --- |
| `read_file` | Read a text file. |
| `view_image` | Return a local PNG, JPEG, GIF or WebP file as native MCP image content, with the original bytes. |
| `rg` | Search file contents with typed ripgrep options. |
| `fd` | Find files with typed fd options. |
| `skill` | Load a skill's instructions and file list. |
| `apply_patch` | Apply a patch to files. |
| `command_exec` | Run a shell command or an argv process. |
| `python_exec` | Run Python in a managed subprocess, with the `yoke_mcp` helper. |
| `process_input` | Write input to a running process and collect a short response. |
| `process_read` | Wait for or page output from up to 16 processes. |
| `process_cancel` | Terminate a process tree and keep its final output. |
| `agent_runs` | List Yoke SDK agent runs started by managed commands. |
| `mcp_inspect` | List downstream MCP servers and their tool schemas. |
| `mcp_call` | Call one downstream MCP tool. |
| `batch_read` | Run up to 16 independent reads or searches in one call. |
| `result_read` | Page through a large retained result. |
| `search_then_read` | Search and return one match window per file. |
| `workspace_snapshot` | Return Git status, root instructions and selected paths. |
| `check_patch` | Apply a patch after hash checks, then run named checks. |
| `import_files` | Download files that ChatGPT passes as file parameters. |
| `write_binary_file` | Upload bytes in chunks. |
| `export_file` | Download bytes in pages. |

A reviewed wrappers file can add `downstream_*` tools. See
[mcp-composition.md](mcp-composition.md#curated-downstream-wrappers).

Results carry an output schema and `structuredContent` with a short text
acknowledgement. Every reply, including image replies, has `yoke/request_id`
and `yoke/version` in MCP `_meta`. Set `YOKE_MCP_LEGACY_RESULT_TEXT=true` for
clients that also need the full JSON as text. Process results always include
the JSON text so clients can see cursors.

`view_image` resolves paths like the other file tools. It rejects unsupported,
invalid, oversized or excessively large decoded images before returning
anything.

### Search tools

`rg` and `fd` take typed arguments, not command-line strings. `rg` accepts
pattern lists, paths, globs, file types, match and result modes, context lines,
ignore behaviour, sorting and a global `limit`. `fd` accepts a pattern, search
paths, types, extensions, excludes, depth, time and size filters, ignore
behaviour, sorting, a `limit` and an optional regex over returned paths. You
cannot express pipelines or switches that start subprocesses. Use
`command_exec` for those. MCP `rg` ignores `RIPGREP_CONFIG_PATH`.

`rg` returns structured matches with paths, line numbers and submatches, path
lists in file modes, or `{path, count}` objects in count mode. `fd` returns
paths, or `path`, `type`, `size_bytes` and `modified_at` with `details: true`.
`limit` replaces `| head`.

### Skills

`skill` discovers `SKILL.md` files under each `--skill-dir` on every call, so
added, changed and removed skills show up without a restart. It returns the
skill's instructions and the absolute paths of every file in its directory.
Configured directories win over Yoke's built-in skills with the same name.
Built-in skills remain as fallbacks. An invalid `SKILL.md` is skipped and does
not break the rest of the catalog.

### Agent runs

`agent_runs` reads the Yoke SDK agent runs that managed `command_exec` and
`python_exec` launches registered. Its optional `session_id` is the process
handle those tools returned, not an OS pid or a conversation ID.
`active_only: true` selects running runs with live observation. `limit` bounds
the result, and `total` and `truncated` describe the matching rows before it.

Each row has separate agent and run IDs, execution status, observation health,
model, name, parent links and the last tool. Rows exclude prompts, process
output and exception messages. Reading does not change agent state or advance
output cursors. Use `process_read` to collect the script's output and exit
status. Stale or lost observation does not prove that a worker stopped. See
[sdk.md](sdk.md) for agent identity and lifecycle.

## Commands and errors

The process tools share one contract with native Yoke, described in
[process-tools.md](process-tools.md). This section covers what is specific to
MCP.

`command_exec` takes exactly one command form:

```json
{"cmd": "pwd"}
```

```json
{"argv": ["git", "status", "--short"], "workdir": "/srv/projects/my-app"}
```

`cmd` is shell text, never an array. `argv` is a non-empty array of non-empty
strings and skips the shell. `command` is a deprecated alias for `cmd`.
Passing both aliases, both forms, or an unknown argument such as `timeout`
returns `INVALID_ARGUMENT`. The tool schema is a plain object. The server checks
the cross-field rules at runtime. MCP commands use a non-login shell unless the
call sets `login: true`.

Both execution tools default to `mode: "auto"`, which waits up to the server's
`--default-yield-ms` for the process to finish. `mode: "background"` returns at
once. Either way a process result includes its `session_id` and `cursor`, even
when the process finished during the call. `yield_time_ms` and `wait_ms` are
rejected on launch. Use `process_read` with `wait_ms` to wait longer.

Argument errors return `stage: "input_validation"`,
`execution_started: false`, a `request_id`, field-level details without the
input values, and a corrected example. Nothing ran, so fix the call and retry.
Do not report an argument error as a permission denial. A client may also
reject an invalid call before sending it, and then only its own transcript
shows the rejection.

Execution errors have their own codes:

| Code | Meaning |
| --- | --- |
| `COMMAND_EXIT_NONZERO` | The command ran and exited nonzero. The exit code is included. |
| `COMMAND_TIMEOUT` | The managed process hit its deadline. |
| `COMMAND_CANCELLED` | The process was cancelled. |
| `OS_PERMISSION_DENIED` | The OS raised `PermissionError`. |
| `TOOL_ERROR`, `TOOL_EXECUTION_ERROR` | Any other failure. |
| `INVALID_TOOL_RESULT` | The result could not be encoded. |

`execution_started: null` means the call was dispatched but the server cannot
tell whether a process started or partial work happened. Inspect state before
retrying. An error after dispatch never proves that a retry is safe.

## Configuration

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--root` | `YOKE_MCP_ROOT` | current directory |
| `--host` | `YOKE_MCP_HOST` | `127.0.0.1` |
| `--port` | `YOKE_MCP_PORT` | `8765` |
| `--default-yield-ms` | `YOKE_MCP_DEFAULT_YIELD_MS` | `30000` |
| `--python-timeout` | `YOKE_MCP_PYTHON_TIMEOUT` | `180` seconds |
| `--max-output-tokens` | `YOKE_MCP_MAX_OUTPUT_TOKENS` | `20000` |
| `--max-remote-wait-ms` | `YOKE_MCP_MAX_REMOTE_WAIT_MS` | `240000` |
| `--allowed-host` (repeatable) | `YOKE_MCP_ALLOWED_HOSTS` (comma-separated) | loopback hosts |
| `--skill-dir` (repeatable) | `YOKE_MCP_SKILL_DIRS` (platform path separator) | built-in skills only |
| `--json-response` / `--no-json-response` | `YOKE_MCP_JSON_RESPONSE` | `false` |
| `--log-level` | `YOKE_MCP_LOG_LEVEL` | `info` |

`--allowed-host` lists the HTTP `Host` headers the server accepts. Add the
public hostname when a reverse proxy or tunnel forwards it to the loopback
port.

`--max-remote-wait-ms` caps how long one execution or `process_read` call may
block. It accepts 5,000 to 240,000, so calls return before the five-minute
deadline common in connectors. Native Yoke's one-hour read limit does not apply
to MCP. `--default-yield-ms` sets the auto-mode wait and is bounded by the cap.

Streamable HTTP responses use SSE by default. During a long tool call the MCP
SDK sends periodic keepalives, which stop reverse proxies from closing idle
connections. Use `--json-response` only for clients that need one buffered
JSON response.

## Downstream MCP gateway

`mcp_inspect` and `mcp_call` reach MCP servers configured for Yoke in
`~/.yoke/mcp.json` and `<root>/.yoke/mcp.json`. A workspace entry overrides a
global entry with the same name. The gateway respects each server's `enabled`,
`enabled_tools` and `disabled_tools`.

Both tools reread the configuration before each call, so adding, changing,
disabling or removing a server takes effect without a restart. Unchanged
servers stay connected. When a server's entry changes, the gateway waits for its
active call, closes the old client and connects again on the next call. If a
config file is invalid mid-edit, the gateway returns a reload error and keeps
the last valid configuration until the file is fixed.

Calls to one downstream server run one at a time, because stdio and stateful
HTTP sessions share client state. Calls to different servers can run in
parallel. `mcp_call` is marked mutating, destructive and open-world, because
one generic tool can reach any downstream action. Use `enabled_tools`
allowlists when the remote client should reach only some actions. Schema
pinning, result handles and images are described in
[mcp-composition.md](mcp-composition.md#discovery-results-and-media).

The server's own authentication covers the remote client's access to
`yoke-mcp`. It does not authenticate `yoke-mcp` to downstream servers.
Downstream stdio servers use their configured environment, and Streamable HTTP
servers can use headers from `.yoke/mcp.json`. The gateway does not run an
interactive OAuth flow for downstream servers.

## Authentication

For private tests, set `YOKE_MCP_BEARER_TOKEN` to require a static bearer token
on `/mcp`. `/healthz` stays public. A static token is not MCP OAuth and is not
enough for a published ChatGPT app.

For a private single-user ChatGPT connection, enable the built-in OAuth 2.1
authorization-code flow with PKCE and dynamic client registration:

```sh
YOKE_MCP_OAUTH_ISSUER_URL=https://mcp.example.com
YOKE_MCP_OAUTH_AUTHORIZATION_PASSWORD='use-a-long-random-secret'
YOKE_MCP_OAUTH_STATE_FILE=~/.local/state/yoke-mcp/oauth.json
YOKE_MCP_OAUTH_ALLOWED_REDIRECT_HOSTS=chatgpt.com
```

The issuer URL has no `/mcp` suffix. The issuer URL and password must be set
together. The server mounts the OAuth metadata, protected-resource metadata,
registration, authorization, token and consent routes. You type the password
only on the server's consent page during the ChatGPT OAuth flow.

The state file holds registered clients, access tokens and rotating refresh
tokens, so they survive restarts. The server writes it with mode 0600. Keep it
outside the source checkout and never commit it. Tokens use a generic subject,
so local account names do not appear in OAuth metadata.

Redirect URIs must use HTTPS and a host in
`YOKE_MCP_OAUTH_ALLOWED_REDIRECT_HOSTS`, which defaults to `chatgpt.com`. HTTP
`localhost` callbacks are also accepted, for testing with MCP Inspector.

## Security

The root is the default directory, not a sandbox. Relative paths resolve from
it. Absolute paths and `..` work wherever the operating-system account can
reach. Run the server as a dedicated account whose filesystem and sudo rights
match what the remote client should do. There is no command denylist. OS
permissions, narrow sudo rules, network policy, authentication and the
client's action confirmations are the boundaries.

Child processes inherit the server's environment, so tools see the same
provider keys, API tokens and `PATH` that `yoke-mcp` started with. The server
removes every `YOKE_MCP_*` variable first, so its bearer token, OAuth password
and other settings never reach commands. On POSIX, the server imports the
user's login-shell environment once at startup, so a service-manager launch
behaves like a terminal without sourcing shell files on every call.

`python_exec` and the `yoke_mcp` helper keep the account's normal permissions.
They are not read-only.

The server has one owner. Every authenticated client shares the same processes
and result handles. A chat ID or process ID is not an ownership credential.
Restarting the server ends all live processes and invalidates all handles.

## Logs

The server writes JSON logs. Each tool request produces a `tool_call_started`
and a `tool_call_finished` event, including for validation errors, unknown
tools, exceptions and cancellations. Events carry the request ID, tool name,
Yoke version, server pid, stage, outcome, whether execution started, error code
and duration. A command still running when its first call returns finishes with
outcome `running`.

Logs record the argument count and the types of `cmd`, `argv` and `command`,
never their values. Unknown tool names are redacted. The server never logs
exception text, tracebacks, command bodies, output, environment values, tokens
or file contents. Uvicorn access logs are off, so OAuth query parameters stay
out of the logs.

## Deploy and update

Run `yoke-mcp` and the tunnel or reverse proxy as separate services:

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

Bind `yoke-mcp` to loopback. Configure the tunnel to forward only the intended
public hostname to `http://127.0.0.1:8765`, and keep the tunnel's credentials
out of `/etc/yoke-mcp.env`.

A restart ends live commands and loses their handles. Before updating, check
for running managed commands, and keep the previous release for rollback. After
deploying, run `initialize`, `tools/list` and `tools/call` through the real
endpoint with authentication. Check the reported Yoke version, the command
schema, an invalid-argument response, a successful command and the paired log
events. A passing health check does not prove the tools work.

ChatGPT caches tool metadata per conversation. For a developer-mode
connection, open it in ChatGPT's plugin settings, select Refresh, confirm the
metadata changed, and test in a new conversation. Published plugins need their
reviewed metadata snapshot updated. See OpenAI's
[connection testing instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata).
