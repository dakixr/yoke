# yoke

Terminal-native coding agent CLI and Python SDK.

Use `yoke` for interactive terminal sessions, one-shot automation, or embedded
agent workflows in Python. It can inspect and edit files, run commands, search
the web, use MCP servers, and call custom tools from your repo or home config.

## Install

```sh
uv tool install git+https://github.com/dakixr/yoke.git
```

For local development:

```sh
uv sync
uv run yoke --help
```

## CLI

```sh
# Start an interactive session
yoke

# Run a one-shot task
yoke --headless "Fix the failing tests"

# Resume the latest session for this repo
yoke continue
```

Interactive sessions support slash commands, session history, image input, model
selection, skills, MCP configuration, and tool inspection.

## SDK

```python
from yoke.ai import Agent, OpenCodeGoConfig, OpenCodeGoProvider, RunConfig

agent = Agent(
    provider=OpenCodeGoProvider(OpenCodeGoConfig(api_key="...")),
    config=RunConfig(root="."),
)

result = agent.prompt("Summarize this repository")
print(result.output)
```

## Capabilities

- **Tools:** file reads and edits, shell commands, web fetch/research, image
  handling, document extraction, and patch application.
- **MCP:** stdio and Streamable HTTP servers configured from `.yoke/mcp.json` or
  `~/.yoke/mcp.json`.
- **Skills:** reusable Markdown instructions loaded by name for repeatable
  workflows.
- **Providers:** Codex, Codex WebSockets, OpenCode Go, Z.ai, and
  OpenAI-compatible APIs.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pyright
```

Documentation lives in `src/yoke/docs`.
