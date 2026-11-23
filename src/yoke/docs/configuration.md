# Configuration

This page covers providers and credentials, `config.json`, tool policy, custom
tools, MCP servers, `AGENTS.md` and environment variables. For everyday CLI use
see [CLI](cli.md).

## File locations

| Path | Contents |
| --- | --- |
| `~/.yoke/config.json` | Global default model and tool policy. |
| `.yoke/config.json` | Workspace default model and tool policy. |
| `~/.yoke/mcp.json`, `.yoke/mcp.json` | MCP servers. |
| `~/.yoke/AGENTS.md`, `AGENTS.md` | Instructions added to every system prompt. |
| `~/.yoke/skills/`, `.yoke/skills/` | Skills. See [CLI](cli.md#use-skills). |
| `~/.yoke/*.py`, `~/.yoke/tools/`, `.yoke/*.py`, `.yoke/tools/` | Custom tool plugins. |
| `~/.yoke/providers/*.py` | Custom provider plugins. |
| `~/.yoke/providers/credentials.json` | Keys stored by `yoke login`. |
| `~/.yoke/providers/logs/` | Codex request logs. |
| `~/.yoke/yoke.db` | Sessions and usage records. See [Sessions](sessions.md). |
| `~/.yoke/daemon.json` | Daemon pid, socket, port and token. |
| `~/.yoke/run/daemon.log` | Log of a detached daemon. |
| `.env` in the workspace root | Extra environment variables for that workspace's sessions. |

Paths starting with `.yoke/` or without `~` are relative to the workspace root.

## Providers and credentials

Yoke has three built-in providers.

| Provider | Service | Credentials |
| --- | --- | --- |
| `codex` | ChatGPT Codex subscription | `yoke login codex`, `~/.codex/auth.json`, the account vault in `~/.codex-auth/accounts/`, or `YOKE_CODEX_API_KEY` |
| `opencode-go` | OpenCode Go | `OPENCODE_API_KEY` or `yoke login opencode-go` |
| `zai` | Z.ai Coding Plan | `ZAI_API_KEY` or `yoke login zai` |

```bash
yoke login codex          # store credentials for a provider
yoke providers list       # providers and whether they are ready
yoke providers doctor     # check credentials and model catalogs
```

The CLI sends your shell environment to the daemon when it attaches, so keys set
in the shell that runs `yoke` reach the provider. A `.env` file in the workspace
root adds more variables. Shell values win over `.env` values.

### Models

| Selection | Context window | Thinking efforts | Default effort |
| --- | --- | --- | --- |
| `codex:gpt-6.2-sol` | 400,000 | `low` `medium` `high` `xhigh` `max` | `medium` |
| `codex:gpt-6.1-sol` | 400,000 | `low` `medium` `high` `xhigh` `max` | `medium` |
| `codex:gpt-6.1-terra` | 400,000 | `none` `low` `medium` `high` `xhigh` `max` | `medium` |
| `codex:gpt-6-luna` | 400,000 | `none` `low` `medium` `high` `xhigh` `max` | `medium` |
| `opencode-go:deepseek-v4.1-flash` | 400,000 | `low` `high` `max` | `high` |
| `opencode-go:muse-spark-1.3-contributor` | 400,000 | `minimal` `low` `medium` `high` `xhigh` | `high` |
| `zai:glm-5.4` | 400,000 | `low` `high` `max` | `max` |
| `zai:glm-5.3-flash` | 400,000 | `low` `high` `max` | `max` |

Every listed model accepts images. A provider selection without a model uses
the provider's default:

| Provider | Default |
| --- | --- |
| `codex` | `codex:gpt-6.1-sol:medium` |
| `opencode-go` | `opencode-go:deepseek-v4.1-flash:high` |
| `zai` | `zai:glm-5.3-flash:max` |

The context windows are Yoke's limits. Some upstream models advertise more, but
Yoke budgets, compacts and reports usage against 400,000 tokens. Codex model
availability depends on your account. Older Codex models such as `gpt-6-sol`
and the `gpt-5.6` family no longer appear in lists, but configs and sessions
that already use them keep working.

Built-in providers wait up to 15 minutes for a response or for the next piece of
a stream. Connection attempts time out sooner. Retries, rate limits and
reconnects show as warnings in the session.

### Provider-specific tools

Codex adds hosted image generation and runs `web_research` through hosted web
search. Several research calls in one model response run in parallel. Other
providers use Yoke's own search, fetch and summarize workflow, which needs no
API key.

### OpenCode Go through a gateway

To route OpenCode Go through an OpenAI-compatible gateway, set:

```bash
export YOKE_OPENCODE_GO_BASE_URL=https://gateway.example/v1   # default https://opencode.ai/zen/go/v1
export YOKE_OPENCODE_GO_MODEL_PREFIX=opencode-go              # sends opencode-go/<model> upstream
```

Model selections stay `opencode-go:<model>`. `OPENCODE_API_KEY` is still
required. If the gateway handles authentication itself, set it to any
placeholder.

### Custom providers

A custom provider is a Python module in `~/.yoke/providers/`. Scaffold one with:

```bash
yoke providers init my-provider    # writes ~/.yoke/providers/my-provider.py
```

The module defines `register_provider(context)` and may define
`list_provider_models(context)` to publish a model catalog. A module can define
`CONFIG_CLASS` and `PROVIDER_CLASS` instead. The CLI and the SDK load the same
plugins and credentials. A provider without a catalog accepts any model name.
See [SDK](sdk.md#custom-providers) for the provider interface.

### TLS and request logs

Yoke checks TLS certificates and host names on every outbound HTTPS and WSS
connection it makes. Set `YOKE_DISABLE_TLS=1` to turn that off for providers,
web tools, remote MCP servers, Codex sign-in and quota requests, and MCP file
downloads. The values `0`, `false`, `no` and `off` leave checks on. In the CLI,
the daemon reads this variable from its own environment. See
[The daemon](cli.md#the-daemon).

Codex writes request logs to `~/.yoke/providers/logs/`. `YOKE_CODEX_LOGS_DIR`
moves them, and `YOKE_PROVIDER_LOGS_DIR` is the fallback for all providers. Each
`request_ok` line records `continuity_mode`. When a request could not continue
the previous response, it also records a `continuity_reason` such as
`request_changed:instructions`, `account_changed` or `no_response_anchor`.
Reasons name keys, positions and item types, never content.

## config.json

`config.json` sets the default model and the tool policy. Every key is optional.

```json
{
  "default_model": "codex:gpt-6.1-sol",
  "default_reasoning_effort": "medium",
  "capabilities": {
    "shell": "allow",
    "mcp": "allow",
    "web.fetch": "deny"
  },
  "tools": {
    "repo_echo": "allow"
  }
}
```

| Key | Value |
| --- | --- |
| `default_model` | `provider:model`. `yoke models set` writes it. |
| `default_reasoning_effort` | An effort the model supports. |
| `capabilities` | Capability ID to `"allow"` or `"deny"`. |
| `tools` | Exact tool name to `"allow"` or `"deny"`. Overrides the capability. |

Yoke merges, in order, its built-in defaults, `~/.yoke/config.json` and the
workspace `.yoke/config.json`. Later files win key by key. Unknown keys and
invalid JSON are errors, and the message names the file and the problem.

## Tool policy

Built-in tools come in capabilities. Each capability resolves to concrete tools
for the current provider and model.

| Capability | Tools | Default |
| --- | --- | --- |
| `file.read` | `read`, `extract_file_context` | allow |
| `file.write` | `apply_patch` for GPT models, `edit` and `write` for others | allow |
| `file.search` | `rg` and `fd` when installed, otherwise `grep`, `find` and `ls` | allow |
| `image.attach` | `attach_image`, for models that accept images | allow |
| `image.generate` | `image_generation`, Codex only | allow |
| `web.fetch` | `web_fetch` | allow |
| `web.search` | `web_search` | allow |
| `web.research` | `web_research` | allow |
| `shell` | `command_exec`, `python_exec`, `process_input`, `process_read`, `process_cancel` | deny |
| `mcp` | `mcp_inspect`, `mcp_call` | deny |

`shell` and `mcp` are off until you allow them. Yoke adds the `skill` tool
whenever it finds at least one skill. Denying a capability also removes the
system instructions that came with its tools.

Edit policy from the command line:

```bash
yoke tools list                               # inventory, with each tool's capability
yoke tools activate shell                     # .yoke/config.json
yoke tools activate mcp --global              # ~/.yoke/config.json
yoke tools deactivate web.fetch --repo
yoke tools deactivate repo_echo --tool        # exact tool override
```

`activate` and `deactivate` write to the workspace `.yoke/config.json` unless you
pass `--global`. `--tool` writes an exact tool name instead of a capability ID.
`yoke tools list` warns about unknown capability IDs and about tool overrides
that match no loaded tool, and exits non-zero if tool discovery fails.

In a session, `/tools` turns individual tools on or off. Yoke then asks whether
the change applies to this session only, to the workspace config or to the
global config. Saving a capability row also clears stale per-tool overrides for
the tools in that row.

Capability IDs and tool names are exact strings. Glob patterns are not
supported. If a config file has an old glob key in `tools`, such as `"*"`, Yoke
replaces that file with the default capability policy.

A read-only agent:

```json
{
  "capabilities": {
    "file.write": "deny",
    "web.fetch": "deny",
    "web.research": "deny",
    "shell": "deny"
  }
}
```

### Process tools

`command_exec` runs a shell command with `cmd`, or a program with no shell with
`argv`. Use `argv` when you already have the arguments split, especially for
values with shell metacharacters. The shell is `YOKE_SHELL`, then `SHELL`, then
`bash` on Linux and macOS. On Windows it is `YOKE_SHELL`, then PowerShell.

Both `command_exec` and `python_exec` wait up to 30 seconds for the process by
default and then return it as still running. Use `process_read` to wait longer.
Its wait defaults to 60 seconds and can be up to one hour. Use `process_input`
to send input and `process_cancel` to stop the process tree. [Shared process tools](process-tools.md)
documents cursors, paging and result fields, and the migration from the old
`exec_command`, `exec_python`, `write_stdin` and `process_io` names.

`python_exec` uses Yoke's own interpreter, or the active `VIRTUAL_ENV` or
`CONDA_PREFIX` of the shell. Pass `python_executable` to use another interpreter
for one call. Child processes get `YOKE_PYTHON_EXECUTABLE` so nested scripts can
find the same interpreter.

On Windows, Yoke rewrites Bash-style Python heredocs such as `python - <<'PY'`
into PowerShell pipelines, and PowerShell pipelines use UTF-8 without a byte
order mark. A native command counts as successful when `LASTEXITCODE` is 0, so
harmless stderr output does not fail the tool call.

## Custom tools

Yoke loads Python tool plugins from:

- files directly in `.yoke/` or `~/.yoke/`, such as `.yoke/tools.py`;
- any file under `.yoke/tools/` or `~/.yoke/tools/`.

Yoke skips files whose names start with `_`. It does not look in other
subdirectories of `.yoke/`, so it never imports caches or skill helper scripts.
When the workspace is your home directory, Yoke loads `~/.yoke/` once. If a
plugin fails to import, Yoke skips it and prints a warning naming the file.

Plugin tools appear next to the built-in ones. They are on unless the `tools`
policy denies them by name.
`yoke tools init` writes an example plugin to `.yoke/example_tools.py`.

### Functions

`@function_tool` turns a typed function into a tool. The function name is the
tool name and the docstring is its description.

```python
# .yoke/tools.py
import subprocess

from yoke.cli.tools.decorators import function_tool


@function_tool
def notify(message: str, title: str = "yoke") -> dict:
    """Send a desktop notification."""
    subprocess.run(["notify-send", title, message])
    return {"ok": True}


@function_tool(name="send_alert", description="Pop up a desktop alert.")
def alert(message: str) -> dict:
    subprocess.run(["notify-send", message])
    return {"ok": True}
```

Every parameter needs a type annotation. `*args` and `**kwargs` are not allowed.
The function must return a `dict`.

### Classes

For validation or workspace access, subclass `WorkspaceTool` and mark it with
`@class_tool`. A tool that does not touch files can subclass `LocalTool`.
Fields are Pydantic fields.

```python
# .yoke/tools.py
from pydantic import Field

from yoke.agent.tools import WorkspaceTool
from yoke.cli.tools.decorators import class_tool


@class_tool
class AppendFileTool(WorkspaceTool):
    name = "append_file"
    description = "Append text to a file, creating it if needed."

    path: str = Field(description="File path relative to the workspace.")
    content: str = Field(description="Text to append.")

    def execute(self) -> dict:
        target = self._resolve_path(self.path, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(self.content)
        return {"ok": True, "path": self._display_path(target)}
```

`@class_tool(name=..., description=...)` overrides the class attributes.

### register_tools

A module that defines `register_tools(context)` returns its tools from that
function, and Yoke does not scan it for decorated tools. Use it when a tool
depends on credentials or on the active model.

```python
# .yoke/tools.py
def register_tools(context):
    api_key = context.home.joinpath(".my_tool_key").read_text().strip()
    if context.model_id and context.model_id.startswith("gpt"):
        return [FastApiTool.bind(root=context.root, api_key=api_key)]
    return [MyApiTool.bind(root=context.root, api_key=api_key)]
```

`context` has `root`, `home`, `provider`, `provider_name`, `model_id`,
`model_name`, `model_key`, `reasoning_effort` and `cancel_requested`. Yoke calls
`register_tools` again when the session switches provider or model.

## MCP servers

Configure MCP servers in `~/.yoke/mcp.json` and `.yoke/mcp.json`. A workspace
server replaces a global server with the same name. The `mcp` capability is off
by default, so run `yoke tools activate mcp` first.

```json
{
  "mcp_servers": {
    "github": {
      "command": "github-mcp-server",
      "args": ["stdio"],
      "env_vars": ["GITHUB_TOKEN"]
    },
    "internal": {
      "url": "https://mcp.internal.example/mcp",
      "headers": {"Authorization": "Bearer ..."},
      "verify": false
    }
  }
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `command`, `args`, `cwd` | | Start a stdio server. |
| `url` | | Connect to a Streamable HTTP server. Setting `url` selects HTTP. |
| `headers` | | HTTP headers. |
| `env` | | Environment values for a stdio server. |
| `env_vars` | `[]` | Names to copy from the session environment into a stdio server. |
| `enabled` | `true` | Turn the server off without deleting it. |
| `enabled_tools`, `disabled_tools` | all, none | Limit which tools the agent sees. |
| `startup_timeout_sec` | 10 | Connection timeout. |
| `tool_timeout_sec` | 60 | Per-call timeout. |
| `verify` | `true` | Check the HTTP server's TLS certificate. `YOKE_DISABLE_TLS=1` overrides it. |

The key `mcpServers` works too.

A stdio server gets only a small base environment: `PATH`, home and user
variables, locale, terminal and shell, temporary directories, XDG directories,
proxy and CA certificate variables, and Windows system paths. Add other names to
`env_vars`, or set values in `env`. Values in `env` win.

The agent sees two tools, `mcp_inspect` and `mcp_call`, instead of every remote
tool, so large catalogs do not fill the context. Yoke rereads the config files
before each call. It reuses a client whose config is unchanged, and closes a
changed or removed server's client after its current call finishes. If a config
file becomes invalid, Yoke reports the error and keeps using the last valid
config.

`yoke mcp` lists servers and their tools. In a session, `/mcp` turns servers and
tools on or off for the session, the workspace or globally. Session-only
changes do not touch either file.

## AGENTS.md

Yoke adds `~/.yoke/AGENTS.md` and the workspace root's `AGENTS.md` to the system
prompt, in that order. Each file is cut to 20,000 characters.

```markdown
# AGENTS.md

This is a FastAPI project. Use async functions throughout.
Run the tests with `make test`.
Never edit migration files directly.
```

## Environment variables

Provider keys and other per-session settings come from the client's environment
and the workspace `.env`. Daemon settings come from the environment the daemon
started in.

| Variable | Read by | Effect |
| --- | --- | --- |
| `OPENCODE_API_KEY` | session | OpenCode Go key. |
| `ZAI_API_KEY` | session | Z.ai key. |
| `YOKE_CODEX_API_KEY` | session | Codex API key, instead of subscription sign-in. |
| `YOKE_OPENCODE_GO_BASE_URL` | session | OpenCode Go endpoint. |
| `YOKE_OPENCODE_GO_MODEL_PREFIX` | session | Prefix added to OpenCode Go model names upstream. |
| `YOKE_CODEX_LOGS_DIR`, `YOKE_PROVIDER_LOGS_DIR` | session | Request log directory. |
| `YOKE_SHELL` | session | Shell for `command_exec`. |
| `YOKE_DISABLE_TLS` | daemon | `1` turns off TLS checks. |
| `YOKE_DB_PATH` | daemon | Session database file. |
| `YOKE_SESSION_DIR` | daemon | Upload directory, and the database's directory. |
| `YOKE_DAEMON_PORT` | daemon | Loopback port. Default 8770. An explicit port that is in use fails startup. |
| `YOKE_DAEMON_IDLE_GRACE` | daemon | Seconds to wait after the last client leaves. Default 5. |
| `YOKE_HTTP_TOKEN` | `yoke serve` | Bearer token. Generated when unset. |
| `YOKE_HTTP_SESSION_DIR` | `yoke serve` | Session store directory. |
| `YOKE_ACP_URL`, `YOKE_ACP_TOKEN` | `yoke acp` | Use a remote HTTP server instead of the local daemon. |
| `YOKE_MERMAID` | terminal | `0` shows Mermaid blocks as code. |
| `YOKE_BAR_TIMER`, `YOKE_BAR_GAUGE`, `YOKE_BAR_TOOLS`, `YOKE_BAR_TOKENS`, `YOKE_BAR_TURN` | terminal | Toolbar segments. See [CLI](cli.md#bottom-toolbar). |

Yoke sets these for the processes its tools start:

| Variable | Value |
| --- | --- |
| `YOKE_PYTHON_EXECUTABLE` | The interpreter `python_exec` used. |
| `YOKE_ROOT_SESSION_ID` | The session that started the process. |
| `YOKE_PARENT_RUN_ID` | The SDK run that started the process, if any. |

The SDK reads every variable from its own process environment.
