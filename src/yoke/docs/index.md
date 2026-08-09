# Yoke

Yoke is a local-first coding-agent CLI and Python SDK. It combines a fast,
stateful agent runtime with provider-aware capabilities, durable sessions,
structured observability, and automatic usage accounting.

## Use Yoke

- [CLI](cli.md): interactive sessions, resumable work, and headless commands.
- [SDK](sdk.md): embedded agents, direct completions, async batches, and durable
  role agents.

## Design

### Providers and models

Yoke ships three provider families:

- `codex`: Codex subscription authentication over the Responses WebSocket
  transport, including hosted web search and image generation.
- `opencode-go`: OpenCode Go models through their native Responses or
  OpenAI-compatible transports.
- `zai`: Z.ai Coding Plan access for GLM models.

Global provider plugins under `~/.yoke/providers/` can add other services.
Provider catalogs advertise model context windows, thinking controls, image
input support, and model-specific system messages. Yoke refreshes capabilities
when the active provider or model changes, so the model sees only tools it can
actually use.

### Capabilities and tools

Capabilities are stable, high-level IDs such as `file.read`, `file.write`,
`file.search`, `web.search`, `web.research`, `image.attach`, and
`image.generate`. Their concrete tools are selected at runtime:

- GPT-family models receive `apply_patch`; other models receive `edit` and
  `write`.
- Native `rg` and `fd` are used when installed, with portable search fallbacks.
- Image attachment is removed for models known not to accept images.
- Image generation is exposed only by a compatible Codex provider.
- Codex web research uses hosted Responses web search; other providers use
  Yoke's local keyless search, fetch, and synthesis workflow.

The CLI also discovers Python tools from `.yoke/` and `~/.yoke/`. MCP servers
are represented by a compact inspect/call facade so large remote tool catalogs
do not consume the model context until needed.

### Sessions and performance

CLI sessions are append-oriented JSONL event streams under
`~/.yoke/sessions/`. They retain conversation branches, provider/model state,
tool activity, compaction checkpoints, titles, and pins. Response continuity,
prompt-cache affinity, context projection, and bounded compaction keep long
sessions efficient without discarding the canonical event history.

SDK agents can save portable state snapshots and autosave durable roles. The
async API serializes calls on one stateful agent, while `run_many()` creates a
fresh isolated agent and provider for every bounded fan-out task.

### Usage and observability

Every completed provider response is recorded automatically in daily JSONL
files under `~/.yoke/usage-metric-logs/<provider>/`. Records include normalized
token usage and execution attribution for CLI and SDK calls. Writes are local,
durably synchronized across concurrent Yoke processes, and can be redirected
with `YOKE_USAGE_METRIC_LOG_DIR`. Persistent write failures are raised rather
than silently dropping metrics.

SDK observers provide `quiet`, `messages`, `actions`, and `full` trace levels.
Console, logging, JSONL, and composite observers share the same event stream,
including provider retries, tool work, cancellation, compaction, and batch
attempts.

## Quick start

```bash
yoke
yoke --headless "Add type annotations to src/utils.py"
```

```python
from yoke.ai import Agent, build_builtin_provider

agent = Agent(
    provider=build_builtin_provider("codex:gpt-5.5:medium"),
)

result = agent.prompt("Add type annotations to src/utils.py")
print(result.output)
agent.close()
```

Use `await agent.prompt_async(...)` in asyncio applications. Use `run_many()`
for independent parallel tasks and attach a `ConsoleObserver("actions")` when
live progress matters.
