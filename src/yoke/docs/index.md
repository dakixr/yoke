# Yoke

Yoke is a coding agent you run from the terminal or embed in Python. It reads
and edits files, runs commands, searches the web, uses skills and MCP servers,
and keeps every conversation as a session you can resume or branch.

The `yoke` command is a client of a per-user daemon that the first command
starts for you. The daemon runs the turns and saves sessions in SQLite at
`~/.yoke/yoke.db`, so the terminal, the web UI and editor integrations all see
the same sessions. The Python SDK runs agents in your own process.

## Install

Yoke needs Python 3.12 or newer.

```bash
uv tool install git+https://github.com/dakixr/yoke.git
yoke version
```

To work on Yoke itself:

```bash
uv sync
uv run yoke --help
```

## Quickstart

Connect a provider. Use one of these:

```bash
yoke login codex                  # ChatGPT Codex subscription
export OPENCODE_API_KEY=...       # OpenCode Go
export ZAI_API_KEY=...            # Z.ai Coding Plan
```

Check that Yoke sees it:

```bash
yoke providers doctor
yoke models list
```

Start a session in your project:

```bash
cd ~/src/my-app
yoke
```

Type a request and press `Enter`. While the agent works, press `Enter` to
redirect it, `Tab` to queue a follow-up, or `Esc` twice to stop it. Run `/help`
for commands and `?` for keyboard shortcuts.

Run one task without the interactive UI:

```bash
yoke --headless "add type annotations to src/utils.py"
```

Come back to earlier work:

```bash
yoke resume
```

By default the agent can read, edit and search files and use the web. Shell
commands are off until you allow them:

```bash
yoke tools activate shell
```

Use the SDK from Python:

```python
from yoke.ai import Agent, build_builtin_provider

agent = Agent(provider=build_builtin_provider("codex:gpt-6.1-sol:medium"))
result = agent.prompt("Add type annotations to src/utils.py")
print(result.output)
agent.close()
```

## Find what you need

| To do this | Read |
| --- | --- |
| Start sessions, run headless, pick a model, attach images, use skills, learn the keys and slash commands | [CLI](cli.md) |
| Resume, fork, branch, relocate and hand off sessions, and maintain the database | [Sessions](sessions.md) |
| Set up providers, `config.json`, tool policy, custom tools, MCP servers, `AGENTS.md` and environment variables | [Configuration](configuration.md) |
| Embed agents in Python, run tasks in parallel, save agent state, read usage records | [SDK](sdk.md) |
| Drive sessions over HTTP, or run the daemon | [HTTP API](http.md), [HTTP reference](http-reference.md), [Daemon client contract](daemon-client.md) |
| Connect T3 Code or another ACP editor | [ACP and T3 Code](acp.md) |
| Serve Yoke's tools to another agent over MCP | [Tool-only MCP server](mcp-server.md), [Composed MCP work](mcp-composition.md) |
| Understand `command_exec`, `process_read` and the other process tools | [Shared process tools](process-tools.md) |
| Use the web UI and its tool, tree, process and agent views | [Web UI](web-ui.md), [Web inspectors](web-inspectors.md) |
| Change the agent loop, providers, tool execution or storage | [Runtime internals](runtime.md), [CLI session backend](cli-backend.md) |
