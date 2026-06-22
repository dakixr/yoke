# yoke

**yoke** is an agentic AI assistant you run from your terminal or embed in Python code. Give it a task, and it uses tools — reading and editing files, running commands, searching the web — to complete it autonomously.

## Ways to use yoke

**[CLI](cli.md)** — interactive sessions and one-shot headless commands from the terminal.

**[SDK](sdk.md)** — embed yoke in your Python code, automate tasks, build agents.

## Core concepts

### Source layout
yoke groups implementation files by subsystem and keeps prefix-related helpers in
subpackages. The CLI prompt-toolkit implementation lives under
`yoke.cli.interactive.prompt`, interactive queue helpers under
`yoke.cli.interactive.queue`, tool inspection helpers under
`yoke.cli.interactive.tools`, shared selector helpers under
`yoke.cli.runtime.selector`, SDK internals under `yoke.ai.sdk`, Codex providers
under `yoke.ai.providers.codex`, agent-loop tool execution under
`yoke.agent.loop.tools`, and Python tool helpers under `yoke.agent.tools.python`.

### Capabilities and tools
Capabilities are context-aware bundles of tools, selected from the active
provider, model, operating system, and workspace environment. For example,
`file.search` exposes native `rg` when ripgrep is installed and Python fallback
tools otherwise; `file.edit` exposes `apply_patch` for GPT-style models and
`edit` plus `write` for other models.

Tools are the executable actions the agent can call: read a file, edit a file,
run a shell command, search the web. The CLI resolves yoke's built-in
capabilities and auto-discovers additional tools from repo `.yoke/tools/` and
global `~/.yoke/tools/` directories. The SDK can use capabilities, explicit
tools, or legacy tool registration callbacks.
For web research, yoke follows Codex-style context passing: the tool receives a
sanitized recent text tail rather than the raw full conversation, keeping the
previous user turn, bounded assistant context, and current user turn while
excluding system/developer/environment/tool noise. Codex-backed research also
forwards hosted web-search settings such as context size, indexed/live access,
and allowed-domain filters.
On Windows, isolated tool processes use `spawn`; yoke passes only the invoked
tool to the child process and strips runtime-only context such as provider
objects and cancellation callbacks that cannot be pickled.

### Skills
Skills are reusable instruction sets — Markdown files that tell the agent *how* to approach a class of task (code review, writing tests, debugging, etc.). You create them once and activate them by name.

### Sessions
The CLI persists conversation history so you can resume where you left off. Each session is stored under `~/.yoke/sessions/` as an append-oriented `.jsonl` event stream. Use `yoke continue` to resume the latest session for the current directory, or `yoke continue --global` to resume the latest session anywhere.

### Providers
yoke connects to an LLM provider (Codex, Codex WebSockets, OpenCode Go, Z.ai, or any OpenAI-compatible endpoint) to power the agent.

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
