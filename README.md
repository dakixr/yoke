# yoke

Yoke is a coding agent you run from the terminal, drive from an editor, or
embed in Python. It reads and edits files, runs commands, searches the web,
uses skills and MCP servers, and keeps every conversation as a session you can
resume or branch.

## How it fits together

Every `yoke` command talks to one per-user daemon, which the first command
starts for you. The daemon runs the turns and saves sessions in SQLite at
`~/.yoke/yoke.db`. The terminal, the browser UI and editors such as T3 Code all
see the same sessions, so you can start work in one and continue it in another.
On Linux and macOS clients reach the daemon over a private Unix socket; on
Windows they use a loopback port with a bearer token.

The Python SDK is separate. It runs agents inside your own process.

## Install

Yoke needs Python 3.12 or newer.

```sh
uv tool install git+https://github.com/dakixr/yoke.git
yoke version
```

Connect a provider, then check that Yoke sees it:

```sh
yoke login codex                  # ChatGPT Codex subscription
export OPENCODE_API_KEY=...       # or OpenCode Go
export ZAI_API_KEY=...            # or Z.ai Coding Plan

yoke providers doctor
yoke models list
```

## Use it

```sh
cd ~/src/my-app
yoke                                    # interactive session
yoke --headless "fix the failing tests" # one task, then exit
yoke resume                             # pick up earlier work
```

While a turn runs, press `Enter` to steer it, `Tab` to queue a follow-up, or
`Esc` twice to stop. `/help` lists the slash commands and `?` the keys. Shell
commands are off until you run `yoke tools activate shell`.

Other entry points:

| To | Run |
| --- | --- |
| See or manage the daemon | `yoke daemon status`, `yoke daemon restart`, `yoke daemon stop` |
| Open the browser UI | Use the `http` address from `yoke daemon status` with the token in `~/.yoke/daemon.json` |
| Use Yoke from T3 Code or another ACP editor | `yoke acp` (see [ACP and T3 Code](src/yoke/docs/acp.md)) |
| Serve only Yoke's tools to another agent | `yoke-mcp --root /path/to/project` (see [MCP server](src/yoke/docs/mcp-server.md)) |
| Check or maintain the session database | `yoke db check`, `yoke db stats`, `yoke db compact` |

## SDK

```python
from yoke.ai import Agent, build_builtin_provider

agent = Agent(provider=build_builtin_provider("codex:gpt-6.1-sol:medium"))
result = agent.prompt("Summarize this repository")
print(result.output)
agent.close()
```

The SDK also runs tasks in parallel, returns structured output, and saves agent
state. See the [SDK guide](src/yoke/docs/sdk.md).

## Providers

| Provider | Access |
| --- | --- |
| `codex` | ChatGPT Codex subscription, including hosted web search and image generation |
| `opencode-go` | OpenCode Go models, directly or through a gateway such as CLIProxy |
| `zai` | Z.ai Coding Plan GLM models |
| OpenAI-compatible | Any endpoint you register as a custom provider |

## Documentation

The docs live in [`src/yoke/docs`](src/yoke/docs) and ship with the package.
Start at the [index](src/yoke/docs/index.md), which maps tasks to pages:

- [CLI](src/yoke/docs/cli.md), [sessions](src/yoke/docs/sessions.md) and
  [configuration](src/yoke/docs/configuration.md) for everyday use.
- [HTTP API](src/yoke/docs/http.md), [HTTP reference](src/yoke/docs/http-reference.md)
  and [daemon clients](src/yoke/docs/daemon-client.md) for integrations.
- [Runtime internals](src/yoke/docs/runtime.md) and
  [web UI](src/yoke/docs/web-ui.md) for contributors.

## Development

```sh
uv sync
uv run pytest            # full suite, in parallel workers
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pyright
```

Use `uv run pytest -n 0 path/to/test.py::test_name` to debug one test serially.
Tests isolate `HOME`, the daemon socket and the database, so they never touch
your real `~/.yoke`. [tests/README.md](tests/README.md) covers fixtures and
conventions.

The web UI has its own node tests under `scripts/test_web_*.mjs`. CI runs the
ones listed in `.github/workflows/http-contract.yml`, plus a Chrome check of the
assembled inspectors and the TypeScript client contract:

```sh
node --experimental-default-type=module scripts/test_web_session_machine.mjs
cd clients/typescript && npm ci && npm run check
```

Read [AGENTS.md](AGENTS.md) before changing code. It covers module size,
provider stability, versioning and docs.
