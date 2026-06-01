# yoke

**yoke** is an agentic AI assistant you run from your terminal or embed in Python code. Give it a task, and it uses tools — reading and editing files, running commands, searching the web — to complete it autonomously.

## Ways to use yoke

**[CLI](cli.md)** — interactive sessions and one-shot headless commands from the terminal.

**[SDK](sdk.md)** — embed yoke in your Python code, automate tasks, build agents.

## Core concepts

### Tools
Tools are the actions the agent can take: read a file, edit a file, run a
shell command, search the web. The CLI includes built-in tools and
auto-discovers additional tools from repo `.yoke/` and global `~/.yoke/`
directories. The SDK lets you pass exactly the tools you want.

### Skills
Skills are reusable instruction sets — Markdown files that tell the agent *how* to approach a class of task (code review, writing tests, debugging, etc.). You create them once and activate them by name.

### Sessions
The CLI persists conversation history so you can resume where you left off. Each session is stored under `~/.yoke/sessions/` as a `.jsonl` file.

### Providers
yoke connects to an LLM provider (Codex, Codex WebSockets, GitHub Copilot, OpenCode Go, Z.ai, or any OpenAI-compatible endpoint) to power the agent.

## Quick start

```bash
# Interactive
yoke

# One-shot
yoke --headless "Add type annotations to src/utils.py"
```

```python
# Python
from yoke.agent.tools import EditTool, ReadTool
from yoke.ai import Agent, OpenCodeGoConfig, OpenCodeGoProvider, RunConfig

agent = Agent(
    provider=OpenCodeGoProvider(OpenCodeGoConfig(api_key="...")),
    config=RunConfig(root=".", tools=[ReadTool, EditTool]),
)

result = agent.prompt("Add type annotations to src/utils.py")
print(result.output)
```
